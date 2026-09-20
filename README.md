# Surge

Surge is a C++20 Linux HTTP gateway for studying overload control under bursty, mixed-cost workloads. The experimental question is whether deadline-aware, request-class-aware admission can improve on-time completions over fixed and latency-adaptive global concurrency limits. That is a hypothesis; this repository does not claim novelty or results.

Milestone 1 is a correct, deliberately small baseline: one level-triggered epoll reactor, one configured upstream, bounded connections and buffers, prompt overload rejection, monotonic timeouts, and clean signal-driven draining.

## Quick start

Native Linux requires CMake 3.25+, a C++20 compiler, Git, Ninja, and Python 3.11+:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

On macOS, build and test the real Linux/epoll implementation in Docker:

```sh
docker build --target build --load -t surge-dev .
docker run --rm surge-dev ctest --test-dir build --output-on-failure
```

Run a self-contained demo, then request both routes from another terminal:

```sh
docker run --rm -p 8080:8080 surge-dev sh -c \
  'python3 tools/backend.py --port 9000 --slow-ms 250 & exec build/surge --upstream 127.0.0.1:9000'
curl -v http://127.0.0.1:8080/fast
curl -v http://127.0.0.1:8080/slow
```

Run `build/surge --help` for all limits and timeouts. Defaults include 1,024 client connections, 128 upstream connections, 16 KiB request headers, 1 MiB complete upstream responses, a 5-second request-header timeout, a 10-second upstream/client-write inactivity timeout, and a 5-second drain bound. Set `--stats-interval-ms 0` to disable periodic output; final counters are always written to stderr and include client write backpressure events.

## Supported protocol

- HTTP/1.1 origin-form `GET`, exactly one request per client connection.
- A required single `Host` header; the gateway writes the configured upstream authority.
- No request body or request framing headers.
- Controlled HTTP/1.1 upstream responses with exactly one valid `Content-Length`.
- `Connection: close` on both legs.
- Standard hop-by-hop headers and headers named by `Connection` are removed.

The gateway rejects TLS, HTTP/2, HTTP/1.0, absolute-form targets, bodies, `Expect`/interim-response negotiation, chunked transfer coding, close-delimited responses, pipelining, upgrades, and connection pooling. DNS resolution happens synchronously at startup. Complete upstream responses are buffered before client transmission, bounded by `--max-response-bytes`.

## Controlled backend

`tools/backend.py` provides `/fast`, `/slow`, `/fragment`, `/large`, `/hang`, and `/close`. Its semaphore bounds active handlers and returns 503 when full. Delays call `sleep`; they model service latency and are not CPU work. The backend is a correctness fixture, not a load generator, and should not be used to infer gateway throughput.

See [architecture](docs/architecture.md) for ownership and lifecycle details and [roadmap](docs/roadmap.md) for later milestones.
