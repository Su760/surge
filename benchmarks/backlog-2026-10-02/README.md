# Direct-backend backlog comparison

`protocol.json` was saved before starting the six-trial series. This directory
preserves every attempt; no smoke trial, retry, or threshold change is part of
this follow-up. The previous smoke and matrix remain in the original directory.

Build and reproduce from the source commit containing this directory:

```sh
docker build --target build --load -t surge-backlog \
  --build-arg BUILD_TYPE=Release --build-arg SANITIZER= .
docker run --rm surge-backlog ctest --test-dir build --output-on-failure
mkdir -p benchmarks/backlog-reproduction
docker run --rm --cpus 4 --memory 4g \
  --mount type=bind,source="$PWD/benchmarks/backlog-reproduction",target=/results \
  surge-backlog python3 tools/performance.py \
  --output /results --source-commit "$(git rev-parse HEAD)" \
  --direct-only --backend-backlogs 5 256 --rates 1600 --repetitions 3 \
  --warmup-s 5 --duration-s 30 --seed 20261002 \
  --backend-fast-ms 2 --backend-concurrency 64 \
  --max-inflight 256 --timeout-s 2 --docker-cpus 4 --docker-memory 4g \
  --gateway-drain-timeout-ms 5000 --shutdown-margin-s 2 --kill-wait-s 2
```

The recorded source_commit is the pre-edit base for gateway provenance. Exact
measured harness/backend and binary hashes are in environment.json; the backend
and harness were edited before measurement. Docker image ID:
`sha256:46bde3a3e419ad07c1d188573ca2f3d74c8222ee4e47bfc1cce91b7b17d58512`.
Host: macOS 26.6.2, Docker 29.4.0, LinuxKit 6.12.76, arm64; Docker VM exposes
12 CPUs and 8,321,429,504 bytes RAM. Each series container is limited to four CPUs
and 4 GiB. environment.json and per-trial settings record actual cgroup limits,
affinity, resource limits, kernel settings, and counter scope.

Raw `.json.gz` files contain all measured arrival IDs, outcomes and phase times,
measured/drain resources and kernel counters, selected backlog/somaxconn, log
paths, process settings, and structured cleanup outcomes. Warmup summaries are
included; warmup arrival records and counters are excluded. stdout/stderr files
are retained even when empty. Client phase timestamps include event-loop and OS
scheduling delays. Counter deltas cover all sockets in the container network
namespace; they are not per-listener measurements.

## Evidence integrity and stopping point

**The comparison is inconclusive.** All six attempts finished and their original
summaries, diagnostics, cleanup outcomes, and logs are preserved in `index.json`.
Its `file` field is the literal `gateway` in all entries because a filename
variable in the measured harness was shadowed by the subprocess launch loop.
The first two raw outputs were overwritten before the bug was noticed. They
cannot be recovered or independently audited at the arrival-record level.

Raw outputs for trials 3–6 were copied to distinct `.json.gz` files as each trial
finished. During trials 4–6 a host-side watcher read/decoded the current raw
output about once per second to rescue it. Container quota, workload and validity
criteria stayed fixed, but this added host activity is another uncontrolled
scheduling factor. The last temporary `gateway` file was removed only after confirming
it was byte-identical to the named trial-6 raw file. `index-preserved.json` is an
unaltered snapshot after trial 3; `index.json` is the original complete six-entry
index. Neither is silently rewritten to repair its recorded filenames.
`measured-performance.py` is the exact harness snapshot identified by the
recorded hash. Do not use it to run experiments: it contains the filename bug.
The published harness repairs the output variable name; generation, timestamps,
summary calculation, cleanup, and validity checks are unchanged.

`summarize.py` reads all six stored summaries, explicitly flags the two missing
raw files, recomputes all four available summaries (192,000 arrivals), and checks
paired schedules for pairs 2 and 3. It writes `summary.csv` and `analysis.json`:

```sh
python3 benchmarks/backlog-2026-10-02/summarize.py
```

All six measured windows and warmups pass the generator guard (no drops,
p99 dispatch lag <=10ms). Passing the generator guard does not repair the raw
evidence loss. Trial 5's excluded warmup includes 29 client timeouts; its stderr
contains broken-pipe reports without phase timestamps. Measurement counters may
include delayed effects from those warmup connections. Trial 6 has a localized
59.45ms maximum dispatch lag despite passing the p99 guard. See the appended
performance report for per-trial evidence and limits.

Exactly six trials were attempted. No trials were repeated and no generator
criteria were relaxed. Experiments stop here; no gateway change or tuning is
supported by this incomplete series. All old benchmark artifacts are unchanged.
