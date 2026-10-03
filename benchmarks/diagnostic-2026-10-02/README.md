# Benchmark correctness and tail diagnostics

See [the report](../../docs/performance.md#benchmark-correctness-and-tail-diagnostics-2026-10-02)
for measurement definitions, generator validity, conclusions, limitations, and commands.

- `smoke/`: six initial trials, 1s excluded warmup and 2s measurement. The direct
  1600/s trial failed the generator guard; it is retained.
- `smoke-confirm/`: six confirmation trials, 5s excluded warmup and 5s measurement.
  All measured windows passed the guard.
- `matrix/`: eighteen trials, direct / one worker / four workers, 1600 and 2400/s,
  three repetitions, 5s excluded warmup and 30s measurement. No failing trial is
  removed or replaced. Consult the per-trial validity flags before interpreting.
- Each series has readable `environment.json` and `index.json` and compressed
  `*.json.gz` trial records. Trial logs contain readiness, warmup, measurement,
  drain, and shutdown; gateway shutdown counters are process-lifetime totals.
- `host.json` records Docker/host versions and the measurement image identity.
- `summary.csv` has one row for every trial, with counts, percentiles, maxima,
  > 1s counts, phase timings, TCP counters, resource windows, and validity flags.
- `analysis.json` has descriptive aggregates across all three repetitions,
  including generator-limited rows. Those means cannot establish worker scaling.
- `SHA256SUMS` covers evidence and the summary script, excluding itself.

Regenerate the readable summaries from the repository root:

```sh
python3 benchmarks/diagnostic-2026-10-02/summarize.py
```

The audit checks independent offered counts and unique IDs, recomputes summaries
from raw records, checks phase ordering and schedule equality across paths, checks
concurrency and aggregate queue bounds, verifies logs exist, and rejects incomplete
series. It reads results and starts no server or load experiment. Nonzero gateway shutdown status is reported
in `analysis.json.shutdown_warnings` and the CSV; it is not silently discarded. Rewriting summary
files updates their hashes; regenerate `SHA256SUMS` if intentionally changing them.

Arrival IDs are scoped to an individual trial's measured cohort. Warmup is separately
accounted and drained before measurement; its raw request records are not stored.
The SHA in metadata identifies unchanged gateway/backend sources, while SHA-256
hashes identify the actual harness, backend, and measured Release binary.
