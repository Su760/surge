# Milestone 1 execution plan

- [x] Add CMake/CTest, Linux Docker workflow, pinned HTTP parser, and tested RAII/config/protocol foundations.
- [x] Implement the single-threaded level-triggered epoll gateway and controlled backend with end-to-end tests.
- [x] Cover overload limits, timeouts, shutdown drain, malformed input, disconnects, fragmentation, and backpressure.
- [x] Add concise architecture/roadmap/project docs and Linux CI matching the real commands.
- [x] Run debug, CTest integration, AddressSanitizer, UndefinedBehaviorSanitizer, and final scope/diff checks.

Approved by the user's instruction to make a brief plan and execute milestone 1 without stopping after planning.

# Milestone 1 publish plan

- [x] Fetch and verify remote `main`, repository contents, and initial-commit scope.
- [x] Review the five named correctness targets and fix only confirmed defects with focused regressions.
- [x] Reconcile README/roadmap with the final single-reactor behavior and limitations.
- [x] Run final Linux debug, ASan, and UBSan checks from the exact source to publish.
- [x] Stage and inspect the complete initial diff, commit, push `main`, and verify local/remote SHA plus GitHub Actions.

Approved by the user's explicit instruction to finalize and publish milestone 1 to `origin/main`.

# Milestone 2 worker-reactor plan

- [x] Add configurable workers, bounded handoff queues, and process-wide client/upstream leases.
- [x] Give each worker exclusive ownership of its epoll loop, connections, sockets, parsers, buffers, and deadlines.
- [x] Coordinate accept shutdown, queued-socket cleanup, bounded active draining, worker wakeups, and joins.
- [x] Run protocol tests with one and multiple workers plus focused queue, global-limit, churn, and shutdown regressions.
- [x] Add and run separate Linux debug, ASan, UBSan, and TSan checks; update milestone documentation.
- [x] Review the staged diff, commit, push `main` without force, and verify CI for the exact commit.

Approved by the user's instruction to implement, verify, commit, and publish milestone 2.

# Performance baseline milestone

- [x] Add a bounded open-loop fresh-connection harness with reconciled outcomes, latency and resource metrics.
- [x] Add focused accounting and metric tests; run the existing Linux checks once in a cached Release image.
- [x] Run a short pilot, fix three offered rates, and collect three repetitions per direct/one-worker/four-worker condition with alternating order.
- [x] Publish raw trial data, environment and commands, an evidence-based report, and a roadmap update.
- [x] Review the diff and tracked files, commit and push without force, then report CI for the pushed commit.

Authorized by the user's performance-baseline request, including commit and push. Stop before optimization.

# Benchmark correctness and tail diagnosis milestone

- [x] Add independent offered-schedule counts and unique arrival IDs; prove missing, duplicate, and unexpected outcomes fail; register unit tests with CTest.
- [x] Separate measured completions, eventual success, drain throughput/duration/resources; add latency maxima, >1s counts, TCP/header/body timestamps, stderr, kernel counters, and environment limits.
- [x] Validate a Release/no-sanitizer image and smoke trials; retain concurrency/dispatch-lag checks and a constant 64-socket aggregate handoff budget (64 for one worker, 16 each for four).
- [x] Run direct/one/four at 1600 and 2400 requests/s, 5s excluded warmup, 30s measurement, three repetitions with alternating order; preserve compressed raw results and readable summaries.
- [x] Append findings, limitations, and reproduction commands without changing original results; recommend one isolated follow-up experiment and make no gateway behavior changes.
- [x] Run relevant correctness checks and review scope/secrets: 23 Python tests, four Release Linux CTest suites, and an audit of all 1,080,000 measured arrivals passed; no gateway/backend changes.
- [x] Commit and push, then verify GitHub Actions for the exact pushed SHA. Stop after this milestone.

Approved by the user's 2026-10-02 continuation, including implementation, tests, the bounded local matrix, documentation, commit, push, and exact-SHA CI verification. Intended edits: tools/performance.py, tests/test_performance.py, CMakeLists.txt, docs/performance.md, tasks/todo.md; new evidence only under benchmarks/diagnostic-2026-10-02/.

Smoke evidence: the initial direct 1600/s smoke failed the dispatch-lag guard (53.5ms p99) and is retained, not interpreted as a service comparison. All six confirmation trials with 5s warmup and 5s measurement passed the guard (no drops, <2.1ms p99 lag). Main matrix started only after this check. Backend backlog remains its inherited value of five.

Matrix evidence: all 18 trials retained; eight measured windows passed the generator guard, ten failed. Valid direct trials show connect tails and listen/SYN-retry evidence; no clean worker-scaling conclusion. One already generator-limited trial was SIGKILLed during post-measurement teardown, with a possible five-second harness/gateway deadline race; preserve that warning rather than changing runtime behavior. Recommended next experiment changes only direct-backend backlog 5 versus 256.

Implementation and evidence published in `ee776f3da77668b87f30c6686de96f04e786ddc7`; [exact-SHA CI](https://github.com/Su760/surge/actions/runs/37080620100) passed Debug, ASan, UBSan, and TSan, including benchmark-unit in each job. This checklist-only completion update will also be pushed and its exact-SHA CI verified before the final response.

# Cleanup and isolated backlog follow-up

- [x] Add bounded, race-safe process cleanup with explicit outcomes; preserve all cleanup attempts, log closure, and completed results. Add lifecycle regressions.
- [x] Configure backend backlog before listen, preserving default 5; add direct-only paired controls and accurate backlog/somaxconn metadata.
- [x] Run relevant correctness checks and build Release without sanitizers.
- [x] Attempt exactly six direct trials at 1600/s: seeds 20261002, 20261003, 20261004; backlog order 5/256, 256/5, 5/256. Keep 5s warmup, 30s measurement, 2ms service, 64 handlers, 256 generator in-flight, 2s timeout, Docker 4 CPUs/4 GiB, and existing generator guard. Retain all available evidence; no retries or threshold changes. Two raw files were lost; the comparison is inconclusive (details below).
- [x] Append evidence, reproduction commands, limitations, and stopping point; preserve previous results.
- [x] Review scope and staged secrets, commit/push, and verify CI for the exact published implementation SHA; verify the final documentation-only SHA before closing the milestone.

Approved by the user's bounded follow-up. Intended edits: tools/performance.py, tools/backend.py, tests/test_performance.py, docs/performance.md, tasks/todo.md; new evidence under benchmarks/backlog-2026-10-02/. No gateway changes or additional experiments.

Scope correction during execution: the new trial filename variable was shadowed by the subprocess launch loop. The first two raw files were overwritten, while their index summaries, diagnostics, and logs remain. Preserve later raw files without altering the six-trial run; add a filename regression and repair output persistence after measurement. The comparison must be reported as inconclusive, with no retries. Preserve the exact measured harness snapshot and explain its difference from the published harness.

Validation: 34 Python tests and all four Linux Release CTest suites passed after the persistence repair. The audit reconciled 192,000 retained measured arrivals; all 288,000 reported measured outcomes succeeded. Six measured windows and warmups passed the unchanged generator guard; one excluded warmup had 29 backend timeouts. All six cleanup records show TERM exit (-15), no KILL and no cleanup errors. No further measurements.

Implementation and evidence: `350d6794275176a75c8759e4b0d96513502bae8e`, pushed to origin/main. [Exact-SHA CI](https://github.com/Su760/surge/actions/runs/37087629507) passed Debug, ASan, UBSan, and TSan. Scope and staged-file review found no new environment, secret, key, or database files. This documentation-only follow-up will receive its own exact-SHA CI verification before the final response. Stop after this milestone; full raw retention failed for two trials, so the comparison remains inconclusive.

# Evidence-retention closeout

- [x] Reject populated output directories and validate every planned output filename before writes or child launches; preserve empty Docker bind-mount support.
- [x] Create metadata, raw results, cleanup sidecars, and logs exclusively; update this run's index atomically. Prove refusal preserves bytes and prevents execution, collisions fail early, and valid raw files remain readable/distinct.
- [x] Run relevant correctness checks, document the completed fix and paused status, and review scope/secrets.

Approved by the user's focused closeout request. Intended edits: tools/performance.py, tests/test_performance.py, docs/performance.md, tasks/todo.md. Preserve all benchmark evidence and the explicitly inconclusive backlog comparison. No performance experiments or gateway changes. Surge pauses after this closeout; further work requires a new request.

Validation: 42 Python tests passed locally; all four Linux CTest suites passed in the Release image with current harness/tests mounted read-only and in a fresh Debug build. Temporary regression data used a Docker bind mount. The new refusal/overwrite regressions were observed failing before the fix; valid-path tests use mocked measurements and subprocesses. No performance experiment ran. All existing benchmark artifacts and gateway/backend/CI code are unchanged.

Status: **paused** after the completed evidence-retention fix. The backlog comparison remains **inconclusive**. Commit/push and exact-final-SHA CI verification are approved and will be reported in the final closeout; no additional benchmark or gateway work will follow.
