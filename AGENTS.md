# Surge contributor guide

Surge targets Linux and C++20. The acceptor and each worker have explicit thread ownership; every fd must have one RAII owner. Handoff queues and process-wide limits stay bounded, and overload is rejected instead of waiting indefinitely. Keep protocol changes covered by parser and integration tests.

## Build and test

Native Linux:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

macOS uses the Linux Docker workflow because the gateway requires epoll:

```sh
docker build --target build --load -t surge-dev .
docker run --rm surge-dev ctest --test-dir build --output-on-failure
docker build --target build --load -t surge-asan --build-arg SANITIZER=address .
docker run --rm surge-asan ctest --test-dir build --output-on-failure
docker build --target build --load -t surge-ubsan --build-arg SANITIZER=undefined .
docker run --rm surge-ubsan ctest --test-dir build --output-on-failure
docker build --target build --load -t surge-tsan --build-arg SANITIZER=thread .
docker run --rm -e TSAN_OPTIONS=halt_on_error=1 surge-tsan \
  ctest --test-dir build --output-on-failure
```

## Conventions

- Format commits as `<type>: <description>`; do not commit or push unless asked.
- Treat `EINTR`, `EAGAIN`, partial I/O, peer close, and async connect failure as ordinary state transitions.
- Keep socket I/O in the owning worker and outside handoff queue locks.
- Use `std::chrono::steady_clock` for deadlines and Linux timerfd for wakeups.
- Keep the HTTP/1.1 subset strict; ambiguous framing is an error.
- Do not claim benchmark results or novelty without measured evidence.
