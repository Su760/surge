# Roadmap

## Milestone 1 — single-reactor baseline

Status: implemented. Linux debug, AddressSanitizer, and UndefinedBehaviorSanitizer builds pass unit and socket-level integration tests for forwarding, fragmentation, measured client-write backpressure, concurrent fast/slow work, failures, timeouts, half-close and disconnect behavior, descriptor reuse, limits, and bounded shutdown. The implemented protocol remains the strict subset in the README.

## Milestone 2 — worker reactors

Status: implemented. One acceptor hands move-only sockets to configurable worker reactors through bounded mutex-protected queues and eventfd notifications. Client and upstream limits remain process-wide, queued sockets are accounted, shutdown drops queued work and drains active work to a monotonic deadline, and aggregate counters are race-safe. One-worker and four-worker integration suites plus a separate ThreadSanitizer build cover the ownership and coordination paths. This milestone makes no performance claim.

## Milestone 3 — admission policies

Before adaptive admission experiments, use the [fresh-connection baseline](performance.md) to establish direct-backend and one-/four-worker behavior and its limits. Implement comparable fixed, latency-adaptive, and deadline/request-class-aware policies behind one small interface. Acceptance requires identical workload inputs, explicit rejection decisions, policy state observability, and tests for each decision boundary.

## Milestone 4 — reproducible open-loop experiments

Schedule offered arrivals independently of completions; record intended and actual send times; use fixed, published seeds and repeated trials. Report on-time completions relative to all offered requests, rejection and timeout rates, per-class results, latency distributions, CPU, memory, and recovery after bursts. Detect coordinated omission and generator saturation so rejecting most traffic cannot appear to win.

## Milestone 5 — profiling and measured optimization

Capture reproducible CPU and allocation profiles on named workloads before changing hot paths. Accept optimizations only with repeated before/after measurements, preserved correctness tests, and reported uncertainty and resource costs.
