#!/usr/bin/env python3
"""Audit retained trials and write descriptive summaries; never generates load."""

import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import performance


def main():
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    protocol = json.loads((directory / "protocol.json").read_text())
    environment = json.loads((directory / "environment.json").read_text())
    index = json.loads((directory / "index.json").read_text())
    assert len(index) == len(protocol["trials_in_order"]), "incomplete six-trial series"
    for key, path in (("harness_sha256", directory / "measured-performance.py"),
                      ("backend_sha256", ROOT / "tools/backend.py")):
        assert environment[key] == hashlib.sha256(path.read_bytes()).hexdigest(), str(path)
    assert environment["build_type"] == "Release" and environment["sanitizer"] == "none"
    quota, period = map(int, environment["cpu_max"].split())
    assert quota / period == protocol["docker_cpus"]
    assert int(environment["memory_max"]) == int(protocol["docker_memory"].removesuffix("g")) * 1024**3
    settings = {
        "backend_fast_ms": "backend_fast_ms", "backend_max_concurrency": "backend_max_concurrency",
        "max_generator_inflight": "max_generator_inflight", "timeout_s": "timeout_s",
        "warmup_s": "warmup_s", "measured_duration_s": "measurement_s",
        "docker_cpu_limit": "docker_cpus", "docker_memory_limit": "docker_memory"}
    for actual, declared in settings.items():
        assert environment[actual] == protocol[declared], actual
    rows, trials, schedules, commands = [], [], {}, []
    missing_raw, audited_arrivals = [], 0
    for order, (item, declared) in enumerate(zip(index, protocol["trials_in_order"], strict=True), 1):
        filename = f"{item['rate_per_s']:04d}-{item['repetition']}-{item['config']}-backlog{item['backend_listen_backlog']:04d}.json.gz"
        raw_available = (directory / filename).exists()
        if raw_available:
            with gzip.open(directory / filename, "rt") as stream:
                trial = json.load(stream)
            records = trial["records"]
            audited_arrivals += len(records)
        else:
            # Retain original index summaries, never manufacture missing records.
            trial, records = item, []
            missing_raw.append(filename)
        assert trial["config"] == protocol["configuration"]
        assert trial["rate_per_s"] == protocol["rate_per_s"]
        assert trial["seed"] == declared["seed"]
        assert trial["repetition"] == declared["repetition"]
        assert trial["backend_listen_backlog"] == declared["backlog"]
        if raw_available:
            assert trial["gateway_command"] is None
        assert trial["kernel_somaxconn"] == environment["kernel_settings"]["somaxconn"]
        summary = trial["summary"]
        assert summary["scheduled"] == performance.expected_arrivals(trial["rate_per_s"], protocol["measurement_s"])
        assert sum(summary["counts"].values()) == summary["scheduled"]
        if raw_available:
            assert performance.summarize(records, protocol["measurement_s"],
                                         summary["elapsed_through_drain_s"], trial["rate_per_s"]) == summary == item["summary"], "summary mismatch"
        assert trial["warmup"]["scheduled"] == performance.expected_arrivals(
            protocol["rate_per_s"], protocol["warmup_s"])
        command = trial["backend_command"].copy() if raw_available else ["unavailable"]
        if raw_available:
            position = command.index("--listen-backlog") + 1
            assert int(command[position]) == trial["backend_listen_backlog"]
            command[position] = "<only changed variable>"
            commands.append(command)
        if raw_available:
            schedule = sorted((r["arrival_id"], r["scheduled_ms"]) for r in records)
            if trial["seed"] in schedules:
                assert schedules[trial["seed"]] == schedule, "paired schedule mismatch"
            schedules[trial["seed"]] = schedule
        for r in records:
            times = [r[k] for k in ("dispatched_ms", "connected_ms", "header_ms",
                                    "body_complete_ms", "complete_ms") if r[k] is not None]
            assert times == sorted(times), "phase ordering"
        assert trial["diagnostics"]["peak_generator_inflight"] <= protocol["max_generator_inflight"]
        for log in trial["logs"].values():
            assert (directory / log).is_file(), "missing log"
        assert f"backlog={trial['backend_listen_backlog']}" in (directory / trial["logs"]["backend_stdout"]).read_text(), "startup backlog mismatch"
        resources = trial["diagnostics"]["resources"]
        counters = trial["diagnostics"]["kernel"]
        row = {"order": order, "pair": trial["repetition"], "seed": trial["seed"],
               "backlog": trial["backend_listen_backlog"], "file": filename if raw_available else None,
               "raw_available": raw_available,
               "measured_valid": not summary["generator_limited"],
               "warmup_valid": not trial["warmup"]["generator_limited"],
               **{f"warmup_{outcome}": count for outcome, count in trial["warmup"]["counts"].items()},
               "warmup_lag_p99_ms": trial["warmup"]["dispatch_lag"]["p99_ms"],
               "lag_p99_ms": summary["dispatch_lag"]["p99_ms"],
               "lag_max_ms": summary["dispatch_lag"]["max_ms"],
               "peak_inflight": trial["diagnostics"]["peak_generator_inflight"],
               "measured_successes": summary["successes_in_measured_window"],
               "eventual_success_fraction": summary["eventual_success_fraction"],
               "measured_success_per_s": summary["success_per_s_measured_window"],
               "success_per_s_including_drain": summary["success_per_s_including_drain"],
               "drain_s": summary["drain_duration_s"], **summary["counts"]}
        for metric in ("response_latency", "all_dispatched_latency"):
            for field in ("p99_ms", "max_ms", "over_1000ms"):
                row[f"{metric}_{field}"] = summary[metric][field]
        for phase, metric in summary["success_phases"].items():
            for field in ("p99_ms", "max_ms", "over_1000ms"):
                row[f"success_{phase}_{field}"] = metric[field]
        all_connect = performance.latency_metrics([r["connected_ms"] - r["dispatched_ms"]
                                                  for r in records if r["connected_ms"] is not None]) if raw_available else {}
        # All reported outcomes succeeded, but retain the distinction between
        # raw audit and the stored successful-connect summary.
        if not raw_available and summary["counts"]["success"] == summary["scheduled"]:
            all_connect = summary["success_phases"]["tcp_connect"]
        for field in ("p99_ms", "max_ms", "over_1000ms", "samples"):
            row[f"all_connected_{field}"] = all_connect.get(field)
        row["timeouts_before_connect"] = sum(r["outcome"] == "timeout" and r["connected_ms"] is None
                                             for r in records) if raw_available else None
        for phase in ("measurement", "drain"):
            row[f"{phase}_resource_boundary_lag_ms"] = trial["diagnostics"]["resource_boundary_lag_ms"] if phase == "measurement" else None
            for name, metric in resources[phase].items():
                for field in ("cpu_seconds", "cpu_percent_one_core", "peak_rss_bytes"):
                    row[f"{phase}_{name}_{field}"] = metric[field] if metric else None
            for name, value in counters[f"{phase}_delta"]["tcp"].items():
                row[f"{phase}_{name}"] = value
            for name, value in counters[f"{phase}_delta"]["cgroup_cpu"].items():
                row[f"{phase}_cgroup_{name}"] = value
        cleanup = trial["cleanup"]
        row["cleanup_status"] = cleanup["processes"]["backend"]["status"]
        row["backend_returncode"] = cleanup["processes"]["backend"]["returncode"]
        row["forced_kill"] = cleanup["processes"]["backend"]["forced_kill"]
        row["cleanup_errors"] = sum(len(v["errors"]) for v in cleanup["processes"].values()) + len(cleanup["log_close_errors"])
        rows.append(row)
        trials.append(trial)
    assert all(command == commands[0] for command in commands), "more than backlog changed"
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (directory / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    aggregate = {}
    for backlog in protocol["backlogs"]:
        group = [t for t in trials if t["backend_listen_backlog"] == backlog]
        complete_raw = all("records" in t for t in group)
        records = [r for t in group for r in t.get("records", [])]
        aggregate[str(backlog)] = {
            "complete_raw": complete_raw,
            "counts": {outcome: sum(t["summary"]["counts"][outcome] for t in group)
                       for outcome in performance.OUTCOMES},
            "successful_response_latency": performance.latency_metrics(
                [r["complete_ms"] - r["dispatched_ms"] for r in records if r["outcome"] == "success"]) if complete_raw else None,
            "all_connected_latency": performance.latency_metrics(
                [r["connected_ms"] - r["dispatched_ms"] for r in records if r["connected_ms"] is not None]) if complete_raw else None,
            "measurement_counters": {key: sum(t["diagnostics"]["kernel"]["measurement_delta"]["tcp"].get(key, 0)
                                                for t in group)
                                     for key in group[0]["diagnostics"]["kernel"]["measurement_delta"]["tcp"]}}
    analysis = {"comparison_valid": not missing_raw and all(r["measured_valid"] and r["warmup_valid"] for r in rows),
                "conclusion": "inconclusive: first two raw files overwritten; no retries",
                "missing_raw_files": missing_raw,
                "reported_arrivals": sum(t["summary"]["scheduled"] for t in trials),
                "measured_valid_trials": sum(r["measured_valid"] for r in rows),
                "warmup_valid_trials": sum(r["warmup_valid"] for r in rows),
                "audited_arrivals": audited_arrivals,
                "aggregate_descriptive_only": aggregate, "trials": rows}
    (directory / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n")
    print(json.dumps({key: analysis[key] for key in ("comparison_valid", "measured_valid_trials",
                                                   "warmup_valid_trials", "audited_arrivals")}, indent=2))


if __name__ == "__main__":
    main()
