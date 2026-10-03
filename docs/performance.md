# Fresh-connection performance baseline (2026-09-26)

This is a bounded baseline, not a gateway capacity or scaling claim. The measured workload was a single HTTP/1.1 `GET /fast` on a new TCP connection for every request. The controlled Python backend slept for 2 ms per `/fast` request; that sleep models service latency, not CPU work. It returned a five-byte body. Direct, one-worker, and four-worker trials used the same backend settings and Docker CPU quota. Gateway trials used the same process-wide limits: 256 clients, 64 upstream connections, and a 64-socket queue per worker. The direct path has no gateway limits.

## Reproduce

From the repository root, with Docker Desktop running:

```sh
docker build --target build --load -t surge-benchmark --build-arg BUILD_TYPE=Release .
docker run --rm surge-benchmark ctest --test-dir build --output-on-failure
python3 -m unittest discover -s tests -p 'test_performance.py' -v
mkdir -p benchmarks/baseline-2026-09-26
docker run --rm --cpus 4 --memory 4g \
  --mount type=bind,src="$PWD/benchmarks",dst=/results \
  surge-benchmark python3 tools/performance.py \
  --output /results/baseline-2026-09-26 \
  --source-commit 6baca3a32d523c21c5a204b8642921635d486d48 \
  --rates 360 1600 2400 --repetitions 3 --duration-s 4 --warmup-s 1
```

The source commit identifies the unchanged gateway/backend code measured here. The raw [environment](../benchmarks/baseline-2026-09-26/environment.json) records SHA-256 hashes of the actual harness, backend, and Release gateway binary. Each of the [27 trial files](../benchmarks/baseline-2026-09-26/index.json) includes its commands, seed, warmup counts, resource readings, summary, and one record per scheduled arrival. Warmup was excluded from all measured metrics. Configuration order rotated across repetitions and rates; the same seed was used for all three configurations within each rate/repetition. Exploratory rate-selection runs are in `benchmarks/pilot/` and `benchmarks/pilot-high/`; a scheduling timestamp correction followed those pilots, so only the fixed series supports the table below.

The generator schedules arrivals by monotonic clock independently of completions, with reproducible bounded jitter within each rate interval. It limits in-flight requests to 256 and records a `generator_drop` if full. Outcomes reconcile as `scheduled = dispatched + generator drops`, and dispatched requests each have exactly one of: success (HTTP 200 with complete body), HTTP rejection (429/503), other HTTP error, timeout, connection failure, or protocol error. A two-second timeout applies per dispatch. Successful-response latency runs from dispatch to receipt of the complete declared body. Scheduled-arrival latency starts at the intended arrival time, including dispatch lag. Successes/s divides successes by time from the start of the offered window to the final outcome, including post-offer drain. Rates and outcome percentages use all scheduled arrivals as denominator; percentiles use successful responses only. Table percentiles are means of the three trial percentiles, not pooled percentiles. A trial is marked generator-limited if it drops any arrival or has p99 dispatch lag over 10 ms.

## Measured results

| Offered/s | Path         | Successes/s, mean (range) | Success | Rejection | Error/timeout | Response p50 / p95 / p99 ms | Scheduled-arrival p99 ms |
| --------: | ------------ | ------------------------: | ------: | --------: | ------------: | --------------------------: | -----------------------: |
|       360 | Direct       |       359.8 (359.7–359.8) |    100% |        0% |            0% |             3.8 / 4.9 / 5.7 |                      7.6 |
|       360 | One worker   |       359.7 (359.5–359.8) |    100% |        0% |            0% |             4.1 / 5.3 / 6.6 |                      9.0 |
|       360 | Four workers |       359.6 (359.5–359.7) |    100% |        0% |            0% |             4.2 / 5.2 / 6.1 |                      8.1 |
|     1,600 | Direct       | 1,557.5 (1,475.9–1,598.6) |    100% |        0% |            0% |             3.3 / 4.5 / 5.2 |                      6.3 |
|     1,600 | One worker   | 1,598.5 (1,598.4–1,598.7) |    100% |        0% |            0% |             3.3 / 4.4 / 4.9 |                      6.0 |
|     1,600 | Four workers | 1,598.6 (1,598.6–1,598.7) |    100% |        0% |            0% |             3.2 / 4.4 / 5.0 |                      6.0 |
|     2,400 | Direct       | 2,100.9 (1,926.5–2,397.4) |    100% |        0% |            0% |             3.3 / 4.8 / 7.2 |                      8.6 |
|     2,400 | One worker   | 1,970.5 (1,881.0–2,085.2) |  99.32% |     0.67% |         0.01% |            3.2 / 4.7 / 60.6 |                     61.1 |
|     2,400 | Four workers | 1,870.4 (1,790.6–1,965.7) |  98.10% |     1.81% |         0.09% |            3.1 / 4.6 / 10.2 |                     11.2 |

At 2,400/s, direct completed 28,800/28,800 scheduled requests; one worker completed 28,604 with 194 HTTP rejections and two timeouts; four workers completed 28,253 with 521 HTTP rejections and 26 timeouts. There were no connection failures, protocol errors, or generator drops. All 27 trials reconciled, none met the generator-limited criterion, and p99 dispatch lag was at most 3.24 ms. A few roughly one-to-two-second response outliers extended drain time; p99 alone does not show these rare tails. The trial files retain them.

CPU is measured as process CPU time divided by elapsed trial time, where 100% means one full core. Memory is the largest sampled resident set during the trial (50 ms sampling), so a shorter-lived peak may be missed. At 2,400/s, mean CPU and maximum sampled memory across the three repetitions were:

| Path         | Backend CPU / MiB | Gateway CPU / MiB | Generator CPU / MiB |
| ------------ | ----------------: | ----------------: | ------------------: |
| Direct       |      59.0% / 18.7 |                 — |        30.3% / 27.9 |
| One worker   |      45.1% / 18.7 |       12.2% / 3.6 |        24.9% / 28.0 |
| Four workers |      41.7% / 18.8 |       14.7% / 3.8 |        24.5% / 27.1 |

The machine was an arm64 Mac running macOS 26.6.2 with Docker Desktop 29.4.0. Measurements ran on Linux 6.12.76 `aarch64` in an Ubuntu 24.04 build image with GCC 13.3.0, CMake `Release`, and no sanitizer. Docker Desktop exposed 12 cores and 8.32 GB to its VM; every trial container had a four-CPU cgroup quota (`cpu.max=400000 100000`) and 4 GiB memory limit. Backend, gateway, and generator shared that quota. The raw environment file has the full configuration and base seed `20260926`.

## Interpretation and limits

The 360/s and 1,600/s runs are valid direct-versus-gateway overhead comparisons for this short, fresh-connection workload; all offered requests succeeded. They do not measure maximum capacity. At 1,600/s, the one- and four-worker results are effectively flat. At 2,400/s, four workers delivered fewer successes/s and a lower success rate than one worker, despite a lower mean p99 latency among successes. The direct path itself varied from 1,926.5 to 2,397.4 successes/s, so the high-rate runs do not isolate gateway scaling or establish its saturation point.

The observed constraint is in the shared direct-backend/host path as well as the gateway path: direct requests developed long tail outliers and variable drain time, while measured gateway CPU remained well below one core. The most likely explanation is fresh TCP connection handling in the Python backend and local host scheduling, but this is a hypothesis; these measurements do not identify a single bottleneck. The backend uses one accepting server with per-connection threads and a concurrency semaphore. Its 2 ms sleep is latency, not CPU work. Results do not generalize to keep-alive traffic, other response sizes, external networks, or dedicated CPU allocations. No gateway optimization is justified by this baseline alone.

The recommended next experiment is to repeat the same offered schedule against a minimal fixed-response native backend on separately allocated CPUs. That would test whether Python backend and shared-host connection handling account for the high-rate variance before changing Surge.

## Benchmark correctness and tail diagnostics (2026-10-02)

The original report and raw files above are preserved as exploratory evidence.
Its reconciliation checks derived the offered count from returned records, so
those checks could not detect missing or duplicate outcomes. Its throughput and
CPU denominator included drain, and four workers had four times the aggregate
handoff capacity. These limitations qualify the original interpretation; the
new series below uses independent accounting and a controlled queue budget.

### Measurement contract

Schema version 2 offers `floor(rate * duration)` arrivals, one per full rate
interval, with the same bounded jitter as the original workload. Arrival IDs are
integers from zero to expected count minus one, scoped to each trial/window.
Summary generation rejects missing, duplicate, unexpected, or unlabelled IDs,
unknown outcomes, and inconsistent dispatch states. A failed internal request
task invalidates the trial rather than silently removing its outcome. The
correctness tests deliberately inject these cases. CTest registers
`benchmark-unit`, so every existing CI matrix job runs them. CI runs no load
experiments.

`successes_in_measured_window` counts measured-cohort HTTP 200 bodies completed
in `[0, duration)`; measured throughput divides this count by the offered window.
`eventual_success_fraction` includes that cohort's post-window successes and
divides by the independent expected count. `success_per_s_including_drain`
divides eventual successes by elapsed time through final outcomes and socket
cleanup. `drain_duration_s` is elapsed time beyond the offered window. Warmup
has separate accounting, is drained before measurement, and is excluded.
Failures and HTTP 429/503 rejections retain separate counts beside successful
latencies; successful-response percentiles do not describe failed requests.
Latency summaries also retain maximum, sample count, and counts strictly over
1,000 ms. `all_dispatched_latency` includes failures and excludes generator drops.

Each dispatched record preserves dispatch, TCP-connect return, full response
header receipt, declared-body completion, and final-outcome timestamps on a
monotonic clock. Missing phases remain null on failures. Success phase summaries
show dispatch-to-connect, connect-to-header (including sending the request), and
header-to-body. These are client observations, including event-loop scheduling;
they are not kernel packet timestamps or gateway-internal upstream timings.
In gateway trials, client connect measures the gateway socket. Slow upstream
connects appear in the client's connect-to-header phase.

Process CPU and 50 ms RSS samples are split at a timer for the offered-window
boundary, with one shared boundary reading closing measurement and opening
drain. Resource summaries use their observed interval lengths; boundary lag is
recorded because a busy event loop cannot sample at an exact instant. Completion
counts still use the nominal window. RSS peaks shorter than 50 ms can be missed;
CPU ticks and short drains have limited precision. Resource collection excludes
summary generation and gzip serialization. The generator's RSS can retain
Python allocations from preceding trials.

TCP snapshots preserve available listen overflow/drop, SYN retransmit, TCP
retransmit/timeout, reset, backlog-drop, and memory-pressure counters at start,
measurement end, and drain end, with separate deltas. Their scope is **all TCP
sockets in the container network namespace**, not a particular listener, request,
or process. The direct path has only the backend listener; gateway paths have
both listeners, so their counter attribution is ambiguous. These are packet or
kernel event counts, not counts of failed requests. See the
[Linux counter definitions](https://docs.kernel.org/networking/snmp_counter.html).
Cgroup CPU snapshots cover all container processes, with separate measurement
and drain deltas; local throttling counters do not describe host/VM contention
or every ancestor limit. See the
[cgroup v2 CPU interface](https://docs.kernel.org/admin-guide/cgroup-v2.html).

Backend and gateway stdout/stderr go directly to per-trial files, avoiding full
pipe buffers blocking the measured processes. Logs include startup, excluded
warmup, measurement, drain, and shutdown. Gateway shutdown counters are
process-lifetime totals, including readiness probes and warmup, and cannot be
read as measurement-only counts. Empty backend stderr files are retained as
such. Environment metadata records installed Python's inherited backend listen
backlog, kernel listen/TCP settings, CPU quota, effective cpuset, per-process
CPU affinity and resource limits, memory and PID limits, and source/binary hashes.
The main matrix does not change the backend or its backlog.

The `--handoff-budget` option sets the **aggregate** gateway queue budget: 64
sockets total means 64 in the one-worker queue and 16 in each of four queues.
This differs from Surge's `--handoff-queue-capacity`, which is per worker. The
kernel listener queue and the process-wide 256-client / 64-upstream limits are
separate from handoff queues. Partitioning a fixed total queue among workers
also changes how capacity is distributed; the comparison does not isolate CPU
parallelism alone.

### Reproduce the diagnostic series

Run from the repository root with Docker Desktop running. Measurements use
Release with an empty sanitizer option; the harness refuses other builds.
The source SHA below identifies the unchanged gateway/backend sources; the
harness hash in `environment.json` identifies the new measurement code.

```sh
docker build --target build --load -t surge-diagnostic \
  --build-arg BUILD_TYPE=Release --build-arg SANITIZER= .
docker run --rm surge-diagnostic ctest --test-dir build --output-on-failure
mkdir -p benchmarks/diagnostic-2026-10-02
# Initial smoke: --duration-s 2 --warmup-s 1; retained under smoke/.
# Confirmation smoke: --duration-s 5 --warmup-s 5; smoke-confirm/.
docker run --rm --cpus 4 --memory 4g \
  --mount type=bind,src="$PWD/benchmarks",dst=/results \
  surge-diagnostic python3 tools/performance.py \
  --output /results/diagnostic-2026-10-02/smoke-confirm \
  --source-commit c57cf3bda16884b04599f7ed3825b6cfc8a769a2 \
  --rates 1600 2400 --repetitions 1 --duration-s 5 --warmup-s 5 \
  --seed 20261002 --handoff-budget 64
# Run the longer matrix only after checking smoke generator validity.
docker run --rm --cpus 4 --memory 4g \
  --mount type=bind,src="$PWD/benchmarks",dst=/results \
  surge-diagnostic python3 tools/performance.py \
  --output /results/diagnostic-2026-10-02/matrix \
  --source-commit c57cf3bda16884b04599f7ed3825b6cfc8a769a2 \
  --rates 1600 2400 --repetitions 3 --duration-s 30 --warmup-s 5 \
  --seed 20261002 --handoff-budget 64
```

Keep result directories distinct when repeating experiments: trial filenames
are deterministic. Raw trials are gzip-compressed JSON; indices, environment
files, and stdout/stderr are readable text. Each rate/repetition uses the same
schedule seed across all paths. Configuration order alternates and rotates
through all six permutations over the six rate/repetition groups, balancing
first, second, and third positions across the complete matrix. Runs are serial
in one container with no concurrent builds or correctness tests.

### Generator checks before interpretation

The initial 2s smoke direct 1600/s trial was generator-limited: p99 dispatch lag
53.52 ms, maximum 67.22 ms, with a concentrated pause near 1.2s. It had 86
timeouts, no generator drops, and no container CPU-quota throttling. It is
preserved under `smoke/` and excluded from service comparisons. The cause of
that pause was not established. The other five initial smoke trials passed the
guard. All six confirmation trials with 5s warmup and 5s measurement passed:
zero generator drops and measured-window p99 dispatch lag below 2.1 ms.

Passing smoke does not guarantee sustained validity for a 30s run. The longer
matrix records each trial's drops and dispatch lag separately, and any trial
with drops or p99 lag above 10 ms is labelled generator-limited. All requested
matrix trials are retained, including failures of that guard; no failing trial
is silently replaced. Such rows describe a disturbed offered workload and
cannot establish gateway capacity or worker scaling. Large maximum dispatch
lags can also affect short subintervals even when the p99 guard passes.

Read the [evidence index](../benchmarks/diagnostic-2026-10-02/README.md),
[per-trial CSV](../benchmarks/diagnostic-2026-10-02/summary.csv), and
[descriptive aggregates](../benchmarks/diagnostic-2026-10-02/analysis.json).
Regenerate and audit the summaries with:

```sh
python3 benchmarks/diagnostic-2026-10-02/summarize.py
```

To reproduce the original methodology above, use repository revision
`c57cf3bda16884b04599f7ed3825b6cfc8a769a2`; the current harness uses the corrected
schema and aggregate queue semantics, so it cannot recreate the original protocol
just by running the old command against current source.

### Longer matrix results and validity

All 18 requested trials completed and all **1,080,000 measured arrivals** passed
independent ID/count reconciliation. There were 1,047,092 eventual successes,
26,583 HTTP rejections, 2,151 timeouts, and 4,174 generator drops; HTTP errors,
connection failures, and protocol errors were zero. Successful completions
within the measured windows totalled 1,046,569. **Only eight of 18 measured
windows passed the generator guard.** At 1600/s the valid trials were direct
repetitions 1–2, one-worker repetition 3, and all three four-worker repetitions.
At 2400/s only one-worker repetitions 1 and 3 passed. Five warmup windows were
also flagged, separately from the measured windows. In particular, every direct
and four-worker 2400/s measured trial was generator-limited. There is no complete
three-repetition valid comparison across all paths at either rate.

The following tables are **descriptive aggregates of all requested trials,
including invalid offered schedules**, not a capacity or scaling comparison.
Rates and percentiles are means of per-trial metrics; eventual success is the
mean fraction (the same denominator in each repetition). Counts sum all three
repetitions. Maximum latency is the largest successful-response latency across
those repetitions. Per-trial flags and ranges remain in the CSV and raw files.

| Offered/s | Path         | Valid measured windows / 3 | Measured successes/s, mean | Successes/s including drain, mean | Eventual success | Mean drain s |
| --------: | ------------ | -------------------------: | -------------------------: | --------------------------------: | ---------------: | -----------: |
|      1600 | Direct       |                          2 |                     1597.0 |                            1565.1 |          99.833% |        0.625 |
|      1600 | One worker   |                          1 |                     1562.7 |                            1548.4 |          97.690% |        0.292 |
|      1600 | Four workers |                          3 |                     1585.9 |                            1581.7 |          99.133% |        0.085 |
|      2400 | Direct       |                          0 |                     2369.4 |                            2259.7 |          98.822% |        1.487 |
|      2400 | One worker   |                          2 |                     2310.1 |                            2251.7 |          96.288% |        0.805 |
|      2400 | Four workers |                          0 |                     2203.4 |                            2135.0 |          91.884% |        0.986 |

| Offered/s | Path         | HTTP rejections | Timeouts | Generator drops | Successful p50 / p95 / p99 ms | Successful max ms | Successful responses >1s | Drain range s |
| --------: | ------------ | --------------: | -------: | --------------: | ----------------------------: | ----------------: | -----------------------: | ------------: |
|      1600 | Direct       |               0 |      175 |              65 |            3.35 / 4.87 / 8.70 |           1889.11 |                      785 |   0.004–0.978 |
|      1600 | One worker   |            2380 |      146 |             800 |           3.34 / 5.00 / 15.99 |           1986.43 |                      765 |   0.004–0.868 |
|      1600 | Four workers |            1145 |      103 |               0 |            3.27 / 4.74 / 9.25 |           1900.45 |                      486 |   0.003–0.248 |
|      2400 | Direct       |               0 |     1062 |            1483 |          3.14 / 4.82 / 685.45 |           1891.01 |                     2491 |   1.443–1.521 |
|      2400 | One worker   |            7729 |      201 |              89 |          3.10 / 4.89 / 346.54 |           1904.16 |                     1721 |   0.003–1.443 |
|      2400 | Four workers |           15329 |      464 |            1737 |          3.10 / 5.49 / 384.76 |           1892.97 |                     1907 |   0.947–1.043 |

Successful-response percentiles exclude all rejections, timeouts, and generator
drops. That exclusion is substantial in some rows. The CSV additionally reports
maximum and >1s counts for all dispatched outcomes and scheduled-arrival latency,
so a short successful p99 must not be read as a low failure rate. Across the
matrix, dispatch-lag p99 ranged from 1.89 to 28.54 ms; maximum lag reached 325.64 ms.
The in-flight bound was never exceeded; multiple invalid trials reached 256.
Even a passing p99 guard can conceal a localized dispatch pause, so this host and
generator are insufficient for a clean capacity claim. Drops indicate the
bounded in-flight set filling while requests are outstanding; they do not by
themselves prove generator CPU saturation.

### Phase and kernel evidence

Each >1s successful response is classified below by its longest observed phase.
These counts include all requested matrix trials and are diagnostic, with the
validity limitations above. Listen-overflow and SYN-retransmit counts are shown
as measurement / drain deltas; listen drops equalled listen overflows in this
series. A kernel event is not necessarily one distinct request.

| Offered/s | Path         | Slow successes dominated by TCP connect | By connect-to-header | By header-to-body | Listen overflows, measurement / drain | SYN retransmits, measurement / drain |
| --------: | ------------ | --------------------------------------: | -------------------: | ----------------: | ------------------------------------: | -----------------------------------: |
|      1600 | Direct       |                                     785 |                    0 |                 0 |                              2309 / 0 |                             950 / 10 |
|      1600 | One worker   |                                       0 |                  765 |                 0 |                             2522 / 16 |                              929 / 8 |
|      1600 | Four workers |                                       0 |                  486 |                 0 |                              1110 / 0 |                              701 / 1 |
|      2400 | Direct       |                                    2491 |                    0 |                 0 |                           12249 / 510 |                           3317 / 240 |
|      2400 | One worker   |                                       0 |                 1721 |                 0 |                             4030 / 84 |                            2009 / 53 |
|      2400 | Four workers |                                       0 |                 1907 |                 0 |                            4760 / 103 |                           2753 / 134 |

The two generator-valid direct 1600/s trials alone had **479 successful responses
over one second**, all dominated by TCP-connect time, with 1,104 measurement-window
listen overflows and 543 SYN retransmits. Thus the connection tail exists with
no gateway and without a failing generator guard. Over the whole matrix,
all 8,155 >1s successful responses were dominated by either TCP connect (direct)
or connect-to-header (gateway); none was dominated by body receipt. In gateway
trials the client connects to Surge quickly, and an upstream connection stall
would appear in connect-to-header. That is consistent with the backend-side
connection bottleneck seen directly, but the namespace counters do not identify
which listener overflowed in gateway trials. Request/packet correlation and
gateway-internal phase timestamps were not collected.

The installed Python 3.12.3 backend inherited a listen backlog of **five**, with
kernel `somaxconn=4096`, `tcp_max_syn_backlog=512`, and
`tcp_abort_on_overflow=0`. The configured gateway listen backlog was 256 through
its existing max-client setting. Backend thread creation and the single accepting
loop can allow this small backend accept queue to fill during bursts. Overflow
and retransmission counters, together with the direct connect delays, make
**backend accept-queue pressure and TCP retries the leading hypothesis**. They do
not establish that increasing the backlog alone would cure the tails or that
backend CPU/thread scheduling is irrelevant. No larger-backlog experiment was
run in this milestone; the backend source and defaults remain unchanged.

### Resource evidence and stderr

Measurements ran on the same arm64 macOS 26.6.2 / Docker Desktop 29.4.0 host,
Linux 6.12.76, Ubuntu 24.04, GCC 13.3.0, Release without sanitizers. Docker exposed
12 CPUs; the container had `cpu.max=400000 100000` and 4 GiB memory limit. The
backend, gateway, and generator shared that quota and affinity CPUs 0–11; CPUs
were not reserved. The effective cpuset was 0–11, PID limit was unlimited, and
per-process soft/hard open-file limits were 1,048,576. The raw environment and
per-trial process settings preserve the complete limits. The memory figures
below are maximum sampled measurement-window RSS; CPU percentages are means
across repetitions and 100% means one core. These include invalid trial rows.

| Offered/s | Path         | Backend CPU / MiB | Gateway CPU / MiB | Generator CPU / MiB | Mean drain CPU seconds, backend / gateway / generator |
| --------: | ------------ | ----------------: | ----------------: | ------------------: | ----------------------------------------------------: |
|      1600 | Direct       |      54.2% / 20.5 |                 — |        31.4% / 62.9 |                                     0.010 / — / 0.017 |
|      1600 | One worker   |      46.5% / 20.2 |       14.2% / 3.7 |        28.9% / 62.9 |                                 0.010 / 0.003 / 0.010 |
|      1600 | Four workers |      46.9% / 20.0 |       16.3% / 3.8 |        28.4% / 62.9 |                                 0.003 / 0.000 / 0.003 |
|      2400 | Direct       |      65.1% / 21.5 |                 — |        34.4% / 80.6 |                                     0.073 / — / 0.050 |
|      2400 | One worker   |      54.5% / 20.5 |       16.1% / 3.7 |        31.3% / 80.5 |                                 0.017 / 0.007 / 0.027 |
|      2400 | Four workers |      58.5% / 20.4 |       21.6% / 3.9 |        35.8% / 80.5 |                                 0.043 / 0.003 / 0.023 |

Container `nr_throttled` and `throttled_usec` deltas were zero in every measured
window and drain. This provides no evidence of throttling by this container's
own CPU quota; it does not rule out host/VM scheduling pauses, ancestor limits,
Python garbage collection, or burst effects. Aggregate process CPU averages do
not prove that the single accepting thread had spare capacity at every instant.
Resource boundary offsets ranged from −0.95 to +5.33 ms relative to the nominal end;
those signed offsets are retained, and measured completion counts use the exact
nominal boundary. Short-drain CPU percentages can be dominated by CPU tick and
sampling precision; use their CPU seconds as well as the longer-window readings.

The retained backend stderr contains `BrokenPipeError` traces in 13 of the 18
matrix trials, consistent with peers timing out/closing before the backend wrote
its response. The backend did not exit during measurement; it was terminated
with SIGTERM after each trial. Gateway stderr normally retains shutdown totals.
One generator-limited trial, `1600-2-one`, recorded gateway return code `-9` and
has no final gateway counter line. The audit initially rejected that unexpected
exit, then reports it explicitly in `analysis.json.shutdown_warnings` and the
CSV rather than dropping the trial. This teardown occurred after client outcome
and resource collection, so it does not change recorded arrival accounting.
There is a possible pre-existing deadline race: `tools/performance.py:359` waits
five seconds before SIGKILL, while `include/surge/config.hpp:22` configures a
five-second gateway drain deadline. That teardown issue was not changed as part
of this milestone, and the missing shutdown totals cannot be reconstructed.

### Supported conclusion, uncertainty, and next experiment

The benchmark now detects outcome-accounting faults and distinguishes measured
completions, eventual successes, and drain. The diagnostic series supports a
TCP connection/listener-pressure explanation for the long direct tails. It
provides **no clean gateway capacity or worker-scaling conclusion**: ten measured
windows failed generator validity, several passing windows had localized pauses,
and a fixed aggregate budget still partitions queues differently among workers.
The reason for those pauses and the causal contribution of the backlog of five
remain uncertain. Changing Surge's behavior or adding admission policies is not
justified by this evidence. Keep-alive workloads, other body sizes, external
networks, reserved CPUs, and a different backend were not measured.

Recommended next experiment: **direct backend only at 2400/s, compare listen
backlog 5 versus 256, changing only that value**. Keep the same threaded backend,
2 ms service delay, 64-handler limit, four-CPU quota/4 GiB limit, 256 in-flight
generator bound, schedules, 5s warmup, 30s measurement, and three alternating
repetitions per backlog. Require both sides to pass generator validity before
comparing them. Check whether listen overflows, SYN retransmits, and connect-time

> 1s tails fall together. That would test the leading hypothesis without changing
> gateway code or simultaneously changing CPU placement/backend implementation.
> If schedules still fail the guard, preserve and diagnose those failures before
> claiming a backlog effect.

Correctness validation: 23 benchmark unit tests passed locally and the Release
Linux CTest suite passed all four tests (C++ unit, benchmark unit, integration
with one worker, integration with four workers). The original missing/duplicate
regressions were observed failing before the accounting fix. The evidence audit
recomputed summaries, checked every measured ID/count and cross-path schedule,
phase order, concurrency/queue budgets, complete trial sets, and log presence.
Local sanitizer runs were not repeated because gateway/backend sources are
unchanged; the existing CI matrix runs Debug, ASan, UBSan, and TSan correctness
checks, including the newly registered benchmark unit suite, on the pushed SHA.
No performance experiment runs in CI.
