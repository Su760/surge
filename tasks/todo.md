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
