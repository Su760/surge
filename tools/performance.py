#!/usr/bin/env python3
"""Bounded, open-loop, fresh-TCP baseline for the controlled Surge backend.

Run inside the Docker build image. Each scheduled arrival has exactly one outcome;
the event loop never waits for a previous response before scheduling the next one.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import signal
import time


CONFIGS = ("direct", "one", "four")
BACKEND_PORT = 19090
GATEWAY_PORT = 18080
REQUEST = b"GET /fast HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
OUTCOMES = ("success", "http_rejection", "http_error", "timeout", "connection_failure", "protocol_error", "generator_drop")


def percentile(values: list[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def summarize(records: list[dict], duration_s: float, elapsed_s: float) -> dict:
    counts = {outcome: 0 for outcome in OUTCOMES}
    for record in records:
        counts[record["outcome"]] += 1
    scheduled = len(records)
    dispatched = scheduled - counts["generator_drop"]
    assert scheduled == dispatched + counts["generator_drop"]
    assert dispatched == sum(counts[name] for name in OUTCOMES if name != "generator_drop")
    success = [record for record in records if record["outcome"] == "success"]
    lag = [record["dispatched_ms"] - record["scheduled_ms"] for record in records if record["dispatched_ms"] is not None]
    response_latency = [record["complete_ms"] - record["dispatched_ms"] for record in success]
    arrival_latency = [record["complete_ms"] - record["scheduled_ms"] for record in success]
    def metrics(values: list[float]) -> dict:
        return {f"p{p}_ms": percentile(values, p) for p in (50, 95, 99)}
    return {
        "scheduled": scheduled, "dispatched": dispatched, "counts": counts,
        "offered_duration_s": duration_s, "elapsed_through_drain_s": elapsed_s,
        "success_per_s": counts["success"] / elapsed_s,
        "success_rate": counts["success"] / scheduled if scheduled else 0,
        "rejection_rate": counts["http_rejection"] / scheduled if scheduled else 0,
        "error_rate": (counts["http_error"] + counts["timeout"] + counts["connection_failure"] + counts["protocol_error"]) / scheduled if scheduled else 0,
        "generator_drop_rate": counts["generator_drop"] / scheduled if scheduled else 0,
        "dispatch_lag": metrics(lag), "response_latency": metrics(response_latency),
        "scheduled_arrival_latency": metrics(arrival_latency),
        "generator_limited": bool(counts["generator_drop"] or (percentile(lag, 99) or 0) > 10),
    }


def process_usage(pid: int) -> tuple[float, int] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu_s = (int(stat[11]) + int(stat[12])) / ticks
        status = Path(f"/proc/{pid}/status").read_text()
        rss_kib = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:"))
        return cpu_s, rss_kib * 1024
    except (FileNotFoundError, ProcessLookupError, StopIteration):
        return None


async def sample_resources(pids: dict[str, int], samples: dict, stop: asyncio.Event) -> None:
    while not stop.is_set():
        for name, pid in pids.items():
            usage = process_usage(pid)
            if usage is not None:
                samples[name].append(usage)
        await asyncio.sleep(0.05)
    for name, pid in pids.items():
        usage = process_usage(pid)
        if usage is not None:
            samples[name].append(usage)


def resource_summary(samples: dict, elapsed_s: float) -> dict:
    result = {}
    for name, points in samples.items():
        if not points:
            result[name] = None
            continue
        cpu_s = max(0, points[-1][0] - points[0][0])
        result[name] = {"cpu_seconds": cpu_s, "cpu_percent_one_core": 100 * cpu_s / elapsed_s,
                        "peak_rss_bytes": max(point[1] for point in points)}
    return result


async def request_once(port: int, scheduled_ms: float, origin: float, timeout_s: float) -> dict:
    dispatched_ms = (time.perf_counter() - origin) * 1000
    writer = None
    status = None
    try:
        async with asyncio.timeout(timeout_s):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(REQUEST)
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header[:-4].split(b"\r\n")
            parts = lines[0].split(b" ", 2)
            if len(parts) < 2 or parts[0] != b"HTTP/1.1":
                raise ValueError("invalid status line")
            status = int(parts[1])
            lengths = [line.split(b":", 1)[1].strip() for line in lines[1:]
                       if line.lower().startswith(b"content-length:")]
            if len(lengths) != 1:
                raise ValueError("missing or repeated Content-Length")
            length = int(lengths[0])
            if length < 0 or length > 1_048_576:
                raise ValueError("invalid Content-Length")
            await reader.readexactly(length)
        outcome = "success" if status == 200 else "http_rejection" if status in (429, 503) else "http_error"
    except TimeoutError:
        outcome = "timeout"
    except (ConnectionError, OSError):
        outcome = "connection_failure"
    except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        outcome = "protocol_error"
    finally:
        complete_ms = (time.perf_counter() - origin) * 1000
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
    return {"scheduled_ms": scheduled_ms, "dispatched_ms": dispatched_ms,
            "complete_ms": complete_ms,
            "outcome": outcome, "http_status": status}


async def load(port: int, rate: int, duration_s: float, seed: int,
               max_inflight: int, timeout_s: float) -> tuple[list[dict], float]:
    rng = random.Random(seed)
    total = int(rate * duration_s)
    origin = time.perf_counter()
    active: set[asyncio.Task] = set()
    records: list[dict] = []
    for index in range(total):
        scheduled_s = (index + 0.5 + rng.uniform(-0.25, 0.25)) / rate
        target = origin + scheduled_s
        while (remaining := target - time.perf_counter()) > 0:
            await asyncio.sleep(remaining)
        scheduled_ms = scheduled_s * 1000
        if len(active) >= max_inflight:
            records.append({"scheduled_ms": scheduled_ms, "dispatched_ms": None,
                            "complete_ms": None, "outcome": "generator_drop", "http_status": None})
            continue
        task = asyncio.create_task(request_once(port, scheduled_ms, origin, timeout_s))
        active.add(task)
        task.add_done_callback(active.discard)
        task.add_done_callback(lambda completed: records.append(completed.result()))
    if active:
        await asyncio.gather(*active)
    return records, time.perf_counter() - origin


async def wait_port(port: int, process: asyncio.subprocess.Process) -> None:
    for _ in range(100):
        if process.returncode is not None:
            raise RuntimeError(f"process exited before port {port} was ready: {process.returncode}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"port {port} did not become ready")


async def stop_process(process: asyncio.subprocess.Process) -> str:
    if process.returncode is None:
        process.send_signal(signal.SIGTERM)
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        process.kill()
        _, stderr = await process.communicate()
    return stderr.decode(errors="replace")


async def trial(config: str, rate: int, repetition: int, seed: int, args: argparse.Namespace,
                directory: Path) -> dict:
    backend_command = ["python3", "tools/backend.py", "--port", str(BACKEND_PORT),
                       "--max-concurrency", str(args.backend_concurrency),
                       "--fast-ms", str(args.backend_fast_ms)]
    gateway_command = ["build/surge", "--listen-address", "127.0.0.1",
                       "--listen-port", str(GATEWAY_PORT), "--upstream", f"127.0.0.1:{BACKEND_PORT}",
                       "--workers", "1" if config == "one" else "4",
                       "--handoff-queue-capacity", str(args.handoff_capacity),
                       "--max-connections", str(args.max_connections),
                       "--max-upstream-connections", str(args.max_upstreams),
                       "--stats-interval-ms", "0"]
    backend = await asyncio.create_subprocess_exec(*backend_command, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
    gateway = None
    try:
        await wait_port(BACKEND_PORT, backend)
        if config != "direct":
            gateway = await asyncio.create_subprocess_exec(*gateway_command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await wait_port(GATEWAY_PORT, gateway)
        port = BACKEND_PORT if config == "direct" else GATEWAY_PORT
        warmup, _ = await load(port, rate, args.warmup_s, seed + 10_000,
                               args.max_inflight, args.timeout_s)
        warmup_summary = summarize(warmup, args.warmup_s, args.warmup_s + args.timeout_s)
        pids = {"backend": backend.pid, "generator": os.getpid()}
        if gateway:
            pids["gateway"] = gateway.pid
        samples = {name: [] for name in pids}
        stop = asyncio.Event()
        sampler = asyncio.create_task(sample_resources(pids, samples, stop))
        records, elapsed = await load(port, rate, args.duration_s, seed,
                                      args.max_inflight, args.timeout_s)
        stop.set()
        await sampler
        summary = summarize(records, args.duration_s, elapsed)
        result = {"config": config, "rate_per_s": rate, "repetition": repetition,
                  "seed": seed, "backend_command": backend_command,
                  "gateway_command": gateway_command if gateway else None,
                  "warmup": {"scheduled": warmup_summary["scheduled"],
                             "counts": warmup_summary["counts"]},
                  "summary": summary, "resources": resource_summary(samples, elapsed),
                  "records": records}
        name = f"{rate:04d}-{repetition}-{config}.json"
        (directory / name).write_text(json.dumps(result, separators=(",", ":")) + "\n")
        print(f"{name}: success={summary['counts']['success']}/{summary['scheduled']} "
              f"drop={summary['counts']['generator_drop']} lag_p99={summary['dispatch_lag']['p99_ms']:.2f}ms "
              f"throughput={summary['success_per_s']:.1f}/s", flush=True)
        return {"file": name, "config": config, "rate_per_s": rate,
                "repetition": repetition, "summary": summary, "resources": result["resources"]}
    finally:
        if gateway:
            await stop_process(gateway)
        await stop_process(backend)


def environment(args: argparse.Namespace) -> dict:
    def read(path: str) -> str | None:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    cache = read("build/CMakeCache.txt") or ""
    build_configuration = {key: next((line.split("=", 1)[1] for line in cache.splitlines()
                            if line.startswith(key + ":")), None)
                           for key in ("CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER", "CMAKE_CXX_COMPILER_ID",
                                       "CMAKE_CXX_COMPILER_VERSION", "SURGE_SANITIZER")}
    return {"source_commit": args.source_commit,
            "harness_sha256": sha256("tools/performance.py"),
            "backend_sha256": sha256("tools/backend.py"),
            "gateway_binary_sha256": sha256("build/surge"),
            "os": platform.platform(),
            "machine": platform.machine(), "python": platform.python_version(),
            "available_cores": os.cpu_count(),
            "affinity_cores": len(os.sched_getaffinity(0)),
            "cpu_max": read("/sys/fs/cgroup/cpu.max"),
            "memory_max": read("/sys/fs/cgroup/memory.max"),
            "build_type": "Release", "sanitizer": "none",
            "cmake": build_configuration,
            "docker_cpu_limit": args.docker_cpus, "docker_memory_limit": args.docker_memory,
            "backend_fast_ms": args.backend_fast_ms,
            "backend_max_concurrency": args.backend_concurrency,
            "handoff_capacity": args.handoff_capacity,
            "gateway_max_connections": args.max_connections,
            "gateway_max_upstreams": args.max_upstreams,
            "max_generator_inflight": args.max_inflight,
            "timeout_s": args.timeout_s, "warmup_s": args.warmup_s,
            "measured_duration_s": args.duration_s,
            "rates_per_s": args.rates, "repetitions": args.repetitions,
            "seed_base": args.seed}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--rates", type=int, nargs="+", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--duration-s", type=float, default=4)
    parser.add_argument("--warmup-s", type=float, default=1)
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--max-inflight", type=int, default=256)
    parser.add_argument("--timeout-s", type=float, default=2)
    parser.add_argument("--backend-concurrency", type=int, default=64)
    parser.add_argument("--backend-fast-ms", type=float, default=2)
    parser.add_argument("--handoff-capacity", type=int, default=64)
    parser.add_argument("--max-connections", type=int, default=256)
    parser.add_argument("--max-upstreams", type=int, default=64)
    parser.add_argument("--docker-cpus", type=int, default=4)
    parser.add_argument("--docker-memory", default="4g")
    args = parser.parse_args()
    if min(args.rates) <= 0 or args.repetitions <= 0 or args.duration_s <= 0 or args.warmup_s < 0:
        parser.error("rates, repetitions, and duration must be positive; warmup must be nonnegative")
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    metadata = environment(args)
    (directory / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    index = []
    for rate_index, rate in enumerate(args.rates):
        for repetition in range(args.repetitions):
            order = list(CONFIGS)
            shift = (rate_index + repetition) % len(order)
            order = order[shift:] + order[:shift]
            if (rate_index + repetition) % 2:
                order.reverse()
            for config in order:
                seed = args.seed + rate_index * 100 + repetition
                index.append(await trial(config, rate, repetition + 1, seed, args, directory))
                (directory / "index.json").write_text(json.dumps(index, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
