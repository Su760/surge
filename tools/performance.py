#!/usr/bin/env python3
"""Bounded, open-loop, fresh-TCP baseline for the controlled Surge backend.

Run inside the Docker build image. Each scheduled arrival has exactly one outcome;
the event loop never waits for a previous response before scheduling the next one.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import gzip
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


def expected_arrivals(rate: int, duration_s: float) -> int:
    """Offer one jittered arrival per full 1/rate interval, independently of outcomes."""
    if rate <= 0 or not math.isfinite(duration_s) or duration_s < 0:
        raise ValueError("rate must be positive and duration finite and nonnegative")
    return math.floor(rate * duration_s)


def latency_metrics(values: list[float]) -> dict:
    return {**{f"p{p}_ms": percentile(values, p) for p in (50, 95, 99)},
            "max_ms": max(values) if values else None,
            "over_1000ms": sum(value > 1000 for value in values), "samples": len(values)}


def summarize(records: list[dict], duration_s: float, elapsed_s: float, rate: int) -> dict:
    scheduled = expected_arrivals(rate, duration_s)
    if any(type(record.get("arrival_id")) is not int for record in records):
        raise ValueError("every outcome must have an integer arrival_id")
    ids = Counter(record["arrival_id"] for record in records)
    expected_ids = set(range(scheduled))
    missing = expected_ids - ids.keys()
    duplicate = {key for key, count in ids.items() if count > 1}
    unexpected = ids.keys() - expected_ids
    if missing or duplicate or unexpected:
        raise ValueError(f"arrival accounting failed: missing={sorted(missing)[:10]} "
                         f"duplicate={sorted(duplicate)[:10]} unexpected={sorted(unexpected)[:10]}")
    counts = {outcome: 0 for outcome in OUTCOMES}
    for record in records:
        if record["outcome"] not in counts:
            raise ValueError(f"unexpected outcome: {record['outcome']}")
        counts[record["outcome"]] += 1
        dispatched = record["dispatched_ms"] is not None
        if dispatched == (record["outcome"] == "generator_drop"):
            raise ValueError("outcome and dispatch state disagree")
    dispatched_records = [record for record in records if record["dispatched_ms"] is not None]
    success = [record for record in records if record["outcome"] == "success"]
    lag = [record["dispatched_ms"] - record["scheduled_ms"] for record in dispatched_records]
    response_latency = [record["complete_ms"] - record["dispatched_ms"] for record in success]
    arrival_latency = [record["complete_ms"] - record["scheduled_ms"] for record in success]
    measured_successes = sum(record["complete_ms"] < duration_s * 1000 for record in success)
    phase_progress = {}
    for outcome in OUTCOMES:
        group = [record for record in records if record["outcome"] == outcome]
        phase_progress[outcome] = {
            "connected": sum(record["connected_ms"] is not None for record in group),
            "header_received": sum(record["header_ms"] is not None for record in group),
            "body_completed": sum(record["body_complete_ms"] is not None for record in group)}
    phases = {"tcp_connect": ("dispatched_ms", "connected_ms"),
              "connect_to_header": ("connected_ms", "header_ms"),
              "header_to_body": ("header_ms", "body_complete_ms")}
    return {
        "scheduled": scheduled, "dispatched": len(dispatched_records), "counts": counts,
        "offered_duration_s": duration_s, "elapsed_through_drain_s": elapsed_s,
        "drain_duration_s": max(0, elapsed_s - duration_s),
        "successes_in_measured_window": measured_successes,
        "success_per_s_measured_window": measured_successes / duration_s if duration_s else 0,
        "success_per_s_including_drain": counts["success"] / elapsed_s if elapsed_s else 0,
        "eventual_success_fraction": counts["success"] / scheduled if scheduled else 0,
        "rejection_rate": counts["http_rejection"] / scheduled if scheduled else 0,
        "error_rate": (counts["http_error"] + counts["timeout"] + counts["connection_failure"] + counts["protocol_error"]) / scheduled if scheduled else 0,
        "generator_drop_rate": counts["generator_drop"] / scheduled if scheduled else 0,
        "dispatch_lag": latency_metrics(lag), "response_latency": latency_metrics(response_latency),
        "scheduled_arrival_latency": latency_metrics(arrival_latency),
        "all_dispatched_latency": latency_metrics([record["complete_ms"] - record["dispatched_ms"]
                                                   for record in dispatched_records]),
        "success_phases": {name: latency_metrics([record[end] - record[start] for record in success])
                           for name, (start, end) in phases.items()},
        "phase_progress": phase_progress,
        "generator_limited": bool(counts["generator_drop"] or (percentile(lag, 99) or 0) > 10),
    }


def per_worker_capacity(aggregate: int, workers: int) -> int:
    if aggregate <= 0 or workers <= 0 or aggregate % workers:
        raise ValueError("aggregate handoff budget must be positive and divisible by worker count")
    return aggregate // workers


def configuration_order(index: int) -> list[str]:
    order = list(CONFIGS)
    if index % 2:
        order.reverse()
    shift = (index // 2) % len(order)
    return order[shift:] + order[:shift]


def read_optional(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def parse_tcp_counters(text: str, section: str) -> dict:
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.startswith(section + ":") and lines[index + 1].startswith(section + ":"):
            keys, values = line.split()[1:], lines[index + 1].split()[1:]
            return {key: int(value) for key, value in zip(keys, values, strict=True)}
    return {}


def kernel_snapshot() -> dict:
    extended = parse_tcp_counters(read_optional("/proc/net/netstat") or "", "TcpExt")
    tcp = parse_tcp_counters(read_optional("/proc/net/snmp") or "", "Tcp")
    wanted = ("ListenOverflows", "ListenDrops", "TCPSynRetrans", "TCPTimeouts",
              "TCPReqQFullDrop", "TCPBacklogDrop", "TCPAbortOnMemory", "TCPMemoryPressures")
    cpu_stat = read_optional("/sys/fs/cgroup/cpu.stat") or ""
    return {"tcp": {key: extended[key] for key in wanted if key in extended}
                   | {key: tcp[key] for key in ("RetransSegs", "AttemptFails", "EstabResets") if key in tcp},
            "cgroup_cpu": {line.split()[0]: int(line.split()[1]) for line in cpu_stat.splitlines()}}


def counter_delta(before: dict, after: dict) -> dict:
    return {key: after[key] - before[key] for key in before.keys() & after.keys()}


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


def capture_usage(pids: dict[str, int], samples: dict) -> None:
    for name, pid in pids.items():
        usage = process_usage(pid)
        if usage is not None:
            samples[name].append(usage)


async def sample_resources(pids: dict[str, int], windows: dict, phase: list[str],
                           stop: asyncio.Event) -> None:
    while not stop.is_set():
        capture_usage(pids, windows[phase[0]])
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except TimeoutError:
            pass


def resource_summary(samples: dict, elapsed_s: float) -> dict:
    result = {}
    for name, points in samples.items():
        if not points:
            result[name] = None
            continue
        cpu_s = max(0, points[-1][0] - points[0][0])
        result[name] = {"cpu_seconds": cpu_s, "cpu_percent_one_core": 100 * cpu_s / elapsed_s if elapsed_s else 0,
                        "peak_rss_bytes": max(point[1] for point in points)}
    return result


async def request_once(port: int, arrival_id: int, scheduled_ms: float,
                       origin: float, timeout_s: float) -> dict:
    dispatched_ms = (time.perf_counter() - origin) * 1000
    writer = None
    status = None
    connected_ms = header_ms = body_complete_ms = None
    try:
        async with asyncio.timeout(timeout_s):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            connected_ms = (time.perf_counter() - origin) * 1000
            writer.write(REQUEST)
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            header_ms = (time.perf_counter() - origin) * 1000
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
            body_complete_ms = (time.perf_counter() - origin) * 1000
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
    return {"arrival_id": arrival_id, "scheduled_ms": scheduled_ms, "dispatched_ms": dispatched_ms,
            "connected_ms": connected_ms, "header_ms": header_ms,
            "body_complete_ms": body_complete_ms, "complete_ms": complete_ms,
            "outcome": outcome, "http_status": status}


async def load(port: int, rate: int, duration_s: float, seed: int,
               max_inflight: int, timeout_s: float, pids: dict[str, int] | None = None,
               diagnostics: dict | None = None) -> tuple[list[dict], float]:
    rng = random.Random(seed)
    total = expected_arrivals(rate, duration_s)
    active: set[asyncio.Task] = set()
    records: list[dict] = []
    failures: list[Exception] = []
    pids = pids or {}
    windows = {phase: {name: [] for name in pids} for phase in ("measurement", "drain")}
    phase = ["measurement"]
    stop = asyncio.Event()
    start_kernel = kernel_snapshot() if pids else {}
    capture_usage(pids, windows["measurement"])
    origin = time.perf_counter()
    boundary_s = duration_s
    boundary_kernel = {}
    peak_inflight = 0

    async def end_window() -> None:
        nonlocal boundary_s, boundary_kernel
        await asyncio.sleep(max(0, origin + duration_s - time.perf_counter()))
        boundary_s = time.perf_counter() - origin
        capture_usage(pids, windows["measurement"])
        # The same boundary reading closes measurement and opens drain.
        for name, points in windows["measurement"].items():
            if points:
                windows["drain"][name].append(points[-1])
        boundary_kernel = kernel_snapshot() if pids else {}
        phase[0] = "drain"

    def finished(task: asyncio.Task) -> None:
        active.discard(task)
        try:
            records.append(task.result())
        except Exception as error:
            failures.append(error)

    boundary_task = asyncio.create_task(end_window())
    sampler = asyncio.create_task(sample_resources(pids, windows, phase, stop)) if pids else None
    try:
        for index in range(total):
            scheduled_s = (index + 0.5 + rng.uniform(-0.25, 0.25)) / rate
            target = origin + scheduled_s
            while (remaining := target - time.perf_counter()) > 0:
                await asyncio.sleep(remaining)
            scheduled_ms = scheduled_s * 1000
            if len(active) >= max_inflight:
                records.append({"arrival_id": index, "scheduled_ms": scheduled_ms,
                                "dispatched_ms": None, "connected_ms": None, "header_ms": None,
                                "body_complete_ms": None, "complete_ms": None,
                                "outcome": "generator_drop", "http_status": None})
                continue
            task = asyncio.create_task(request_once(port, index, scheduled_ms, origin, timeout_s))
            active.add(task)
            peak_inflight = max(peak_inflight, len(active))
            task.add_done_callback(finished)
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await boundary_task
    finally:
        stop.set()
        if sampler:
            await sampler
        if not boundary_task.done():
            boundary_task.cancel()
            await asyncio.gather(boundary_task, return_exceptions=True)
    elapsed = time.perf_counter() - origin
    capture_usage(pids, windows["drain"])
    end_kernel = kernel_snapshot() if pids else {}
    if failures:
        raise RuntimeError(f"request task failed: {failures[0]}") from failures[0]
    if diagnostics is not None:
        diagnostics.update({"peak_generator_inflight": peak_inflight,
                            "resource_boundary_s": boundary_s,
                            "resource_boundary_lag_ms": (boundary_s - duration_s) * 1000,
                            "resources": {"measurement": resource_summary(windows["measurement"], boundary_s),
                                          "drain": resource_summary(windows["drain"], max(0, elapsed - boundary_s))}})
        if pids:
            diagnostics["kernel"] = {
                "start": start_kernel, "measurement_end": boundary_kernel, "drain_end": end_kernel,
                "measurement_delta": {key: counter_delta(start_kernel[key], boundary_kernel[key])
                                      for key in start_kernel},
                "drain_delta": {key: counter_delta(boundary_kernel[key], end_kernel[key])
                                for key in boundary_kernel}}
    return records, elapsed


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


def shutdown_timeout(drain_timeout_ms: int, margin_s: float) -> float:
    if drain_timeout_ms < 0 or not math.isfinite(margin_s) or margin_s <= 0:
        raise ValueError("drain deadline must be nonnegative and shutdown margin positive/finite")
    return drain_timeout_ms / 1000 + margin_s


def cleanup_error(operation: str, error: Exception) -> dict:
    return {"operation": operation, "type": type(error).__name__, "message": str(error)}


async def stop_process(process: asyncio.subprocess.Process, shutdown_s: float,
                       kill_wait_s: float) -> dict:
    already_exited = process.returncode is not None
    result = {"term_sent": False, "forced_kill": False, "graceful_exit": False,
              "exit_races": [], "errors": [], "reaped": False,
              "shutdown_timeout_s": shutdown_s, "kill_wait_timeout_s": kill_wait_s}

    def send(operation, action):
        try:
            action()
            result["term_sent" if operation == "term" else "forced_kill"] = True
        except ProcessLookupError:
            result["exit_races"].append(operation)
        except Exception as error:
            result["errors"].append(cleanup_error(operation, error))

    if not already_exited:
        send("term", lambda: process.send_signal(signal.SIGTERM))
    try:
        # Logs go to files, so wait() cannot deadlock on a full subprocess pipe.
        await asyncio.wait_for(process.wait(), shutdown_s)
        result["reaped"] = True
    except TimeoutError:
        result["shutdown_deadline_exceeded"] = True
    except Exception as error:
        result["errors"].append(cleanup_error("term_wait", error))
    if not result["reaped"]:
        send("kill", process.kill)
        try:
            await asyncio.wait_for(process.wait(), kill_wait_s)
            result["reaped"] = True
        except Exception as error:
            result["errors"].append(cleanup_error("kill_wait", error))
    result["returncode"] = process.returncode
    result["graceful_exit"] = result["reaped"] and not result["forced_kill"]
    result["status"] = ("cleanup_error" if result["errors"] or not result["reaped"] else
                        "forced_kill" if result["forced_kill"] else
                        "already_exited" if already_exited else "graceful_exit")
    return result


async def cleanup_trial(processes: dict, handles: list, result: dict | None,
                        result_path: Path, shutdown_s: float, kill_wait_s: float) -> dict:
    cleanup = {"processes": {}, "log_close_errors": []}
    try:
        for name in ("gateway", "backend"):
            if name not in processes:
                continue
            try:
                cleanup["processes"][name] = await stop_process(
                    processes[name], shutdown_s, kill_wait_s)
            except Exception as error:
                cleanup["processes"][name] = {
                    "status": "cleanup_error", "reaped": False,
                    "returncode": processes[name].returncode,
                    "errors": [cleanup_error("stop_process", error)]}
    finally:
        for index, handle in enumerate(handles):
            try:
                handle.close()
            except Exception as error:
                cleanup["log_close_errors"].append(
                    cleanup_error(f"close_log_{index}", error))
        # Save completed measurements even when cleanup encountered errors.
        if result is not None:
            result["cleanup"] = cleanup
            result["process_returncodes"] = {name: item["returncode"]
                                             for name, item in cleanup["processes"].items()}
            with gzip.open(result_path, "xt", encoding="utf-8", compresslevel=6) as output:
                json.dump(result, output, separators=(",", ":"))
                output.write("\n")
        else:
            path = result_path.with_name(result_path.name.removesuffix(".json.gz") + ".cleanup.json")
            write_json_exclusive(path, cleanup)
    return cleanup


async def trial(config: str, rate: int, repetition: int, seed: int, args: argparse.Namespace,
                directory: Path, backlog: int = 5) -> dict:
    stem = trial_stem(config, rate, repetition, backlog)
    result_name = stem + ".json.gz"
    workers = 1 if config == "one" else 4
    backend_command = ["python3", "tools/backend.py", "--port", str(BACKEND_PORT),
                       "--max-concurrency", str(args.backend_concurrency),
                       "--fast-ms", str(args.backend_fast_ms), "--listen-backlog", str(backlog)]
    gateway_command = ["build/surge", "--listen-address", "127.0.0.1",
                       "--listen-port", str(GATEWAY_PORT), "--upstream", f"127.0.0.1:{BACKEND_PORT}",
                       "--workers", str(workers),
                       "--handoff-queue-capacity", str(per_worker_capacity(args.handoff_budget, workers)),
                       "--max-connections", str(args.max_connections),
                       "--max-upstream-connections", str(args.max_upstreams),
                       "--stats-interval-ms", "0",
                       "--drain-timeout-ms", str(args.gateway_drain_timeout_ms)]
    processes = {}
    log_files = {}
    handles = []
    result = None
    try:
        for name, command, port in (("backend", backend_command, BACKEND_PORT),
                                    ("gateway", gateway_command, GATEWAY_PORT)):
            if name == "gateway" and config == "direct":
                continue
            streams = {}
            for stream in ("stdout", "stderr"):
                filename = f"{stem}.{name}.{stream}.txt"
                handle = (directory / filename).open("xb")
                handles.append(handle)
                streams[stream] = handle
                log_files[f"{name}_{stream}"] = filename
            processes[name] = await asyncio.create_subprocess_exec(*command, **streams)
            await wait_port(port, processes[name])
        port = BACKEND_PORT if config == "direct" else GATEWAY_PORT
        warmup, warmup_elapsed = await load(port, rate, args.warmup_s, seed + 10_000,
                                           args.max_inflight, args.timeout_s)
        warmup_summary = summarize(warmup, args.warmup_s, warmup_elapsed, rate)
        pids = {name: process.pid for name, process in processes.items()} | {"generator": os.getpid()}
        process_settings = {name: {"affinity": sorted(os.sched_getaffinity(pid)),
                                  "limits": read_optional(f"/proc/{pid}/limits")}
                            for name, pid in pids.items()}
        diagnostics = {}
        records, elapsed = await load(port, rate, args.duration_s, seed,
                                      args.max_inflight, args.timeout_s, pids, diagnostics)
        summary = summarize(records, args.duration_s, elapsed, rate)
        result = {"schema_version": 3, "config": config, "rate_per_s": rate,
                  "repetition": repetition, "seed": seed,
                  "backend_command": backend_command,
                  "gateway_command": gateway_command if config != "direct" else None,
                  "backend_listen_backlog": backlog,
                  "kernel_somaxconn": read_optional("/proc/sys/net/core/somaxconn"),
                  "aggregate_handoff_capacity": args.handoff_budget if config != "direct" else None,
                  "warmup": warmup_summary, "summary": summary,
                  "diagnostics": diagnostics, "process_settings": process_settings,
                  "logs": log_files, "records": records}
    finally:
        await cleanup_trial(processes, handles, result, directory / result_name,
                            shutdown_timeout(args.gateway_drain_timeout_ms, args.shutdown_margin_s),
                            args.kill_wait_s)
    print(f"{result_name}: measured={summary['successes_in_measured_window']}/{summary['scheduled']} "
          f"eventual={summary['counts']['success']} drops={summary['counts']['generator_drop']} "
          f"lag_p99={summary['dispatch_lag']['p99_ms']:.2f}ms "
          f"drain={summary['drain_duration_s']:.3f}s "
          f"throughput_including_drain={summary['success_per_s_including_drain']:.1f}/s "
          f"generator_limited={summary['generator_limited']}", flush=True)
    return {key: result[key] for key in ("config", "rate_per_s", "repetition", "seed",
                                       "backend_listen_backlog", "kernel_somaxconn", "summary",
                                       "warmup", "diagnostics", "logs", "cleanup")} | {"file": result_name}


def environment(args: argparse.Namespace) -> dict:
    cache = read_optional("build/CMakeCache.txt") or ""
    build_configuration = {key: next((line.split("=", 1)[1] for line in cache.splitlines()
                            if line.startswith(key + ":")), None)
                           for key in ("CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER", "CMAKE_CXX_COMPILER_ID",
                                       "CMAKE_CXX_COMPILER_VERSION", "SURGE_SANITIZER")}
    if build_configuration["CMAKE_BUILD_TYPE"] != "Release" or build_configuration["SURGE_SANITIZER"] != "":
        raise ValueError("measurements require a CMake Release build with no sanitizer")
    return {"schema_version": 3, "source_commit": args.source_commit,
            "harness_sha256": sha256("tools/performance.py"),
            "backend_sha256": sha256("tools/backend.py"),
            "gateway_binary_sha256": sha256("build/surge"),
            "os": platform.platform(),
            "machine": platform.machine(), "python": platform.python_version(),
            "available_cores": os.cpu_count(),
            "affinity_cores": len(os.sched_getaffinity(0)),
            "affinity": sorted(os.sched_getaffinity(0)),
            "process_limits": read_optional("/proc/self/limits"),
            "pids_max": read_optional("/sys/fs/cgroup/pids.max"),
            "pids_current": read_optional("/sys/fs/cgroup/pids.current"),
            "cpuset_effective": read_optional("/sys/fs/cgroup/cpuset.cpus.effective"),
            "network_namespace": os.readlink("/proc/self/ns/net"),
            "tcp_counter_scope": "container network namespace, all TCP sockets; not per listener or process",
            "cgroup_counter_scope": "container cgroup, all processes; phase snapshots exclude warmup",
            "kernel_settings": {name: read_optional(f"/proc/sys/{path}") for name, path in {
                "somaxconn": "net/core/somaxconn", "tcp_max_syn_backlog": "net/ipv4/tcp_max_syn_backlog",
                "tcp_syn_retries": "net/ipv4/tcp_syn_retries", "tcp_synack_retries": "net/ipv4/tcp_synack_retries",
                "tcp_abort_on_overflow": "net/ipv4/tcp_abort_on_overflow",
                "ip_local_port_range": "net/ipv4/ip_local_port_range"}.items()},
            "backend_listen_backlogs": args.backend_backlogs,
            "direct_only": args.direct_only,
            "trial_plan": trial_plan(args),
            "gateway_drain_timeout_ms": args.gateway_drain_timeout_ms,
            "shutdown_margin_s": args.shutdown_margin_s,
            "shutdown_timeout_s": shutdown_timeout(args.gateway_drain_timeout_ms, args.shutdown_margin_s),
            "kill_wait_timeout_s": args.kill_wait_s,
            "cpu_max": read_optional("/sys/fs/cgroup/cpu.max"),
            "memory_max": read_optional("/sys/fs/cgroup/memory.max"),
            "build_type": "Release", "sanitizer": "none",
            "cmake": build_configuration,
            "docker_cpu_limit": args.docker_cpus, "docker_memory_limit": args.docker_memory,
            "backend_fast_ms": args.backend_fast_ms,
            "backend_max_concurrency": args.backend_concurrency,
            "aggregate_handoff_capacity": args.handoff_budget,
            "per_worker_handoff_capacity": {"one": per_worker_capacity(args.handoff_budget, 1),
                                            "four": per_worker_capacity(args.handoff_budget, 4)},
            "gateway_max_connections": args.max_connections,
            "gateway_max_upstreams": args.max_upstreams,
            "max_generator_inflight": args.max_inflight,
            "timeout_s": args.timeout_s, "warmup_s": args.warmup_s,
            "measured_duration_s": args.duration_s,
            "rates_per_s": args.rates, "repetitions": args.repetitions,
            "seed_base": args.seed}


def trial_plan(args: argparse.Namespace) -> list[tuple]:
    plan = []
    for rate_index, rate in enumerate(args.rates):
        for repetition in range(args.repetitions):
            seed = args.seed + rate_index * 100 + repetition
            if args.direct_only:
                backlogs = args.backend_backlogs if repetition % 2 == 0 else args.backend_backlogs[::-1]
                plan.extend(("direct", rate, repetition + 1, seed, backlog) for backlog in backlogs)
            else:
                plan.extend((config, rate, repetition + 1, seed, args.backend_backlogs[0])
                            for config in configuration_order(rate_index * args.repetitions + repetition))
    return plan


def trial_stem(config: str, rate: int, repetition: int, backlog: int) -> str:
    return f"{rate:04d}-{repetition}-{config}-backlog{backlog:04d}"


def validate_trial_plan(plan: list[tuple]) -> None:
    seen = set()
    for config, rate, repetition, seed, backlog in plan:
        stem = trial_stem(config, rate, repetition, backlog)
        filenames = [stem + ".json.gz", stem + ".cleanup.json"]
        processes = ("backend",) if config == "direct" else ("backend", "gateway")
        filenames.extend(f"{stem}.{process}.{stream}.txt"
                         for process in processes for stream in ("stdout", "stderr"))
        for filename in filenames:
            if filename in seen:
                raise ValueError(f"duplicate trial output filename: {filename}")
            seen.add(filename)


def write_json_exclusive(path: Path, data) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
        output.write("\n")


def update_index(path: Path, index: list) -> None:
    # This index was exclusively created by the new run. Write beside it so
    # replacement stays on the same filesystem, including Docker bind mounts.
    temporary = path.with_name(f".{path.name}.tmp")
    output = temporary.open("x", encoding="utf-8")
    try:
        with output:
            json.dump(index, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    parser.add_argument("--direct-only", action="store_true")
    parser.add_argument("--backend-backlogs", type=int, nargs="+", default=[5],
                        help="multiple backlogs require --direct-only; paired order alternates")
    parser.add_argument("--gateway-drain-timeout-ms", type=int, default=5000)
    parser.add_argument("--shutdown-margin-s", type=float, default=2,
                        help="TERM wait exceeds the configured gateway drain by this margin")
    parser.add_argument("--kill-wait-s", type=float, default=2)
    parser.add_argument("--handoff-budget", type=int, default=64,
                        help="aggregate socket budget, divided equally among workers")
    parser.add_argument("--max-connections", type=int, default=256)
    parser.add_argument("--max-upstreams", type=int, default=64)
    parser.add_argument("--docker-cpus", type=int, default=4)
    parser.add_argument("--docker-memory", default="4g")
    args = parser.parse_args()
    if min(args.rates) <= 0 or args.repetitions <= 0 or args.duration_s <= 0 or args.warmup_s < 0:
        parser.error("rates, repetitions, and duration must be positive; warmup must be nonnegative")
    if (not math.isfinite(args.duration_s) or not math.isfinite(args.warmup_s)
            or not math.isfinite(args.timeout_s) or args.timeout_s <= 0
            or min(args.max_inflight, args.backend_concurrency, args.max_connections, args.max_upstreams) <= 0):
        parser.error("durations must be finite and limits/timeouts positive")
    try:
        per_worker_capacity(args.handoff_budget, 4)
        shutdown_timeout(args.gateway_drain_timeout_ms, args.shutdown_margin_s)
    except ValueError as error:
        parser.error(str(error))
    if not math.isfinite(args.kill_wait_s) or args.kill_wait_s <= 0:
        parser.error("kill wait must be positive and finite")
    if min(args.backend_backlogs) <= 0 or len(set(args.backend_backlogs)) != len(args.backend_backlogs):
        parser.error("backlogs must be distinct and positive")
    if not args.direct_only and len(args.backend_backlogs) != 1:
        parser.error("multiple backend backlogs require --direct-only")
    plan = trial_plan(args)
    try:
        validate_trial_plan(plan)
    except ValueError as error:
        parser.error(str(error))
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        parser.error(f"refusing nonempty experiment output directory: {directory}")
    metadata = environment(args)
    # Exclusive metadata creation also prevents two starters from claiming
    # the same initially empty directory before either launches a child.
    write_json_exclusive(directory / "environment.json", metadata)
    index = []
    index_path = directory / "index.json"
    write_json_exclusive(index_path, index)
    for config, rate, repetition, seed, backlog in plan:
        completed = await trial(config, rate, repetition, seed, args, directory, backlog)
        index.append(completed)
        update_index(index_path, index)
        if any(not item["reaped"] for item in completed["cleanup"]["processes"].values()):
            raise RuntimeError("cleanup left an unreaped child; saved completed trial and stopping")


if __name__ == "__main__":
    asyncio.run(main())
