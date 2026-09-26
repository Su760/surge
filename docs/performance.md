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
