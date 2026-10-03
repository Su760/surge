#!/usr/bin/env python3
"""Audit preserved diagnostic records and regenerate readable CSV/JSON summaries.

Run from the repository root: python3 benchmarks/diagnostic-2026-10-02/summarize.py
This reads results only; it never starts a benchmark or a server.
"""

from collections import Counter
import csv
import gzip
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))
from tools.performance import OUTCOMES, summarize  # noqa: E402


def check(condition, message):
    if not condition:
        raise ValueError(message)


def collect():
    rows = []
    schedules = {}
    for series in ("smoke", "smoke-confirm", "matrix"):
        directory = ROOT / series
        index = json.loads((directory / "index.json").read_text())
        environment = json.loads((directory / "environment.json").read_text())
        check(len(index) == 3 * len(environment["rates_per_s"]) * environment["repetitions"],
              f"{directory}: incomplete series")
        expected_conditions = {(rate, repetition, config) for rate in environment["rates_per_s"]
                               for repetition in range(1, environment["repetitions"] + 1)
                               for config in ("direct", "one", "four")}
        actual_conditions = {(entry["rate_per_s"], entry["repetition"], entry["config"]) for entry in index}
        check(actual_conditions == expected_conditions, f"{directory}: missing or repeated condition")
        for entry in index:
            path = directory / entry["file"]
            with gzip.open(path, "rt") as source:
                trial = json.load(source)
            summary = trial["summary"]
            check(summary["offered_duration_s"] == environment["measured_duration_s"],
                  f"{path}: measurement duration differs from protocol")
            records = trial["records"]
            offered = int(trial["rate_per_s"] * environment["measured_duration_s"])
            check(sorted(record["arrival_id"] for record in records) == list(range(offered)),
                  f"{path}: missing/duplicate/unexpected ID")
            check(offered == summary["scheduled"], f"{path}: independent offered count mismatch")
            recomputed = summarize(records, summary["offered_duration_s"],
                                   summary["elapsed_through_drain_s"], trial["rate_per_s"])
            check(recomputed == summary == entry["summary"], f"{path}: summary mismatch")
            schedule = {record["arrival_id"]: record["scheduled_ms"] for record in records}
            key = (series, trial["rate_per_s"], trial["repetition"])
            check(key not in schedules or schedules[key] == schedule, f"{path}: offered schedule differs across paths")
            schedules[key] = schedule
            for record in records:
                check(0 <= record["scheduled_ms"] < summary["offered_duration_s"] * 1000,
                      f"{path}: arrival outside offered window")
                times = [record[field] for field in ("dispatched_ms", "connected_ms", "header_ms",
                         "body_complete_ms", "complete_ms") if record[field] is not None]
                check(times == sorted(times), f"{path}: phase timestamps out of order")
            for filename in trial["logs"].values():
                check((directory / filename).is_file(), f"{path}: missing log {filename}")
            diagnostics = trial["diagnostics"]
            check(diagnostics["peak_generator_inflight"] <= environment["max_generator_inflight"],
                  f"{path}: concurrency bound exceeded")
            if trial["config"] != "direct":
                workers = 1 if trial["config"] == "one" else 4
                command = trial["gateway_command"]
                capacity = int(command[command.index("--handoff-queue-capacity") + 1])
                check(workers * capacity == 64 == trial["aggregate_handoff_capacity"],
                      f"{path}: queue budget mismatch")

            counts = Counter(record["outcome"] for record in records)
            row = {"series": series, "trial": entry["file"], "rate_per_s": trial["rate_per_s"],
                   "config": trial["config"], "repetition": trial["repetition"], "expected_arrivals": offered,
                   "measured_successes": summary["successes_in_measured_window"],
                   "measured_success_per_s": summary["success_per_s_measured_window"],
                   "eventual_success_fraction": summary["eventual_success_fraction"],
                   "success_per_s_including_drain": summary["success_per_s_including_drain"],
                   "drain_duration_s": summary["drain_duration_s"],
                   **{outcome: counts[outcome] for outcome in OUTCOMES},
                   "generator_limited": summary["generator_limited"],
                   "warmup_generator_limited": trial["warmup"]["generator_limited"],
                   "peak_inflight": diagnostics["peak_generator_inflight"],
                   "gateway_exit_status": trial["process_returncodes"].get("gateway"),
                   "resource_boundary_lag_ms": diagnostics["resource_boundary_lag_ms"]}
            for name in ("dispatch_lag", "response_latency", "scheduled_arrival_latency", "all_dispatched_latency"):
                row.update({f"{name}_{metric}": value for metric, value in summary[name].items()})
            for name, metrics in summary["success_phases"].items():
                row.update({f"{name}_{metric}": value for metric, value in metrics.items()})
            # Associate a >1s successful response with its longest observed phase.
            slow_phases = Counter()
            for record in records:
                if record["outcome"] != "success" or record["complete_ms"] - record["dispatched_ms"] <= 1000:
                    continue
                phases = {"tcp_connect": record["connected_ms"] - record["dispatched_ms"],
                          "connect_to_header": record["header_ms"] - record["connected_ms"],
                          "header_to_body": record["body_complete_ms"] - record["header_ms"]}
                slow_phases[max(phases, key=phases.get)] += 1
            row.update({f"slow_success_longest_{name}": slow_phases[name]
                        for name in ("tcp_connect", "connect_to_header", "header_to_body")})
            for phase in ("measurement", "drain"):
                for process in ("backend", "gateway", "generator"):
                    resource = diagnostics["resources"][phase].get(process) or {}
                    for metric in ("cpu_seconds", "cpu_percent_one_core", "peak_rss_bytes"):
                        row[f"{phase}_{process}_{metric}"] = resource.get(metric)
                for counter in ("ListenOverflows", "ListenDrops", "TCPSynRetrans", "RetransSegs", "TCPTimeouts"):
                    row[f"{phase}_{counter}"] = diagnostics["kernel"][f"{phase}_delta"]["tcp"].get(counter)
                row[f"{phase}_nr_throttled"] = diagnostics["kernel"][f"{phase}_delta"]["cgroup_cpu"].get("nr_throttled")
            rows.append(row)
    return rows


def main():
    rows = collect()
    with (ROOT / "summary.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    groups = []
    for rate in (1600, 2400):
        for config in ("direct", "one", "four"):
            selected = [row for row in rows if row["series"] == "matrix" and row["rate_per_s"] == rate
                        and row["config"] == config]
            if not selected:
                continue
            group = {"rate_per_s": rate, "config": config, "repetitions": len(selected),
                     "generator_limited_trials": sum(row["generator_limited"] for row in selected),
                     "warmup_generator_limited_trials": sum(row["warmup_generator_limited"] for row in selected)}
            sums = ("expected_arrivals", "measured_successes", *OUTCOMES, "response_latency_over_1000ms",
                    "all_dispatched_latency_over_1000ms", "slow_success_longest_tcp_connect",
                    "slow_success_longest_connect_to_header", "slow_success_longest_header_to_body",
                    "measurement_ListenOverflows", "drain_ListenOverflows", "measurement_ListenDrops",
                    "drain_ListenDrops", "measurement_TCPSynRetrans", "drain_TCPSynRetrans",
                    "measurement_nr_throttled", "drain_nr_throttled")
            group["totals"] = {name: sum(row[name] for row in selected) for name in sums}
            means = ("measured_success_per_s", "success_per_s_including_drain", "eventual_success_fraction",
                     "drain_duration_s", "response_latency_p50_ms", "response_latency_p95_ms", "response_latency_p99_ms",
                     "tcp_connect_p99_ms", "connect_to_header_p99_ms", "header_to_body_p99_ms",
                     "measurement_backend_cpu_percent_one_core", "measurement_gateway_cpu_percent_one_core",
                     "measurement_generator_cpu_percent_one_core", "drain_backend_cpu_seconds",
                     "drain_gateway_cpu_seconds", "drain_generator_cpu_seconds")
            group["means"] = {name: statistics.mean(row[name] for row in selected if row[name] is not None)
                              if any(row[name] is not None for row in selected) else None for name in means}
            ranges = ("measured_success_per_s", "success_per_s_including_drain", "drain_duration_s",
                      "response_latency_max_ms", "dispatch_lag_p99_ms", "dispatch_lag_max_ms", "peak_inflight",
                      "resource_boundary_lag_ms", "measurement_backend_peak_rss_bytes",
                      "measurement_gateway_peak_rss_bytes", "measurement_generator_peak_rss_bytes")
            group["ranges"] = {name: [min(row[name] for row in selected if row[name] is not None),
                                      max(row[name] for row in selected if row[name] is not None)]
                               if any(row[name] is not None for row in selected) else None for name in ranges}
            groups.append(group)
    result = {"audited_trials": len(rows), "aggregations": "means of trial metrics; totals across trials; no discarded matrix trials",
              "groups": groups,
              "shutdown_warnings": [{"series": row["series"], "trial": row["trial"],
                                     "gateway_exit_status": row["gateway_exit_status"]}
                                    for row in rows if row["gateway_exit_status"] not in (None, 0)]}
    (ROOT / "analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
