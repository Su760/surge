# Milestone 2 architecture

One coordinator thread owns the listener, signal fd, coordinator timer, and acceptor epoll instance. Each configured worker thread owns a separate epoll instance, eventfd, timerfd, and every active connection assigned to it. Connection maps, client and upstream sockets, parsers, buffers, offsets, deadlines, and epoll registration tokens are never accessed by another thread.

## Handoff and ownership

The acceptor reserves a process-wide connection lease immediately after `accept4`. That lease counts the socket against `max_connections` while it is queued and moves with the `UniqueFd` through a mutex-protected bounded queue. `eventfd` wakes the selected worker after the queue lock is released. The worker drains the queue into local storage, registers each client in its epoll instance, and performs all network I/O without holding the queue lock.

Queues have a configurable per-worker capacity. The acceptor tries each queue once in round-robin order and returns 503 if all are full; it never waits for a worker. Closing or draining a queue does not copy descriptors. Destroying a queued item, an active `Connection`, or a failed handoff releases its move-only fd and its global connection lease together.

Each upstream attempt similarly acquires a lease from one process-wide `max_upstream_connections` counter. Worker count therefore does not multiply either configured concurrency limit. Atomic aggregate counters contain no pointers into worker-owned state.

## Connection lifecycle

Each worker retains the milestone 1 state machine:

| State               | Interested events       | Worker-owned data                    |
| ------------------- | ----------------------- | ------------------------------------ |
| reading request     | client readable         | bounded llhttp request parser        |
| connecting upstream | upstream writable/error | serialized bounded request           |
| writing upstream    | upstream writable       | request plus write offset            |
| reading upstream    | upstream readable       | bounded llhttp response parser       |
| writing client      | client writable         | sanitized response plus write offset |

Level-triggered handlers repeat nonblocking I/O until completion or `EAGAIN`. Asynchronous connect completion uses `SO_ERROR`. A client write-half-close removes read interest while preserving the response path. Each worker assigns monotonic tokens to registrations, so an event already returned by `epoll_wait` cannot alias a socket that later reuses the same fd number.

## Shutdown and counters

SIGINT or SIGTERM closes the listener, closes every handoff queue to new pushes, wakes all workers, and drops sockets still queued. Already active connections may finish until the shared monotonic drain deadline. Deadline expiry or a second signal wakes workers again and closes all remaining connections. The coordinator polls worker completion, joins every thread, and prints final counters only after ownership has returned to destructors.

`accepted` counts successful kernel accepts. `active` includes queued and worker-active client sockets. `queued` is a synchronized snapshot. `upstream_active` is process-wide. `completed` counts complete client response writes. `rejected` includes overload and protocol rejections; `queue_rejected` is the subset caused by full handoff queues. Upstream errors, timeouts, and client backpressure events retain their milestone 1 meanings. `worker_connections` reports cumulative sockets adopted by each worker and is an observability check, not a performance result.
