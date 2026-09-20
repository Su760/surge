# Milestone 1 architecture

One thread owns the listener, epoll instance, signalfd, timerfd, every accepted client socket, every upstream socket, and all connection state. `UniqueFd` is move-only and closes its descriptor once on destruction. Each epoll registration gets a monotonic token that maps to a connection id and role. Removing a connection erases its tokens, unregisters and closes both legs, then releases buffers; an event already returned by `epoll_wait` therefore cannot alias a later socket that reuses the same fd number.

Each connection moves through one linear state:

| State               | Interested events       | Owned data                           |
| ------------------- | ----------------------- | ------------------------------------ |
| reading request     | client readable         | bounded llhttp request parser        |
| connecting upstream | upstream writable/error | serialized bounded request           |
| writing upstream    | upstream writable       | request plus write offset            |
| reading upstream    | upstream readable       | bounded llhttp response parser       |
| writing client      | client writable         | sanitized response plus write offset |

Level-triggered handlers repeat `accept`, `recv`, or `send` until completion or `EAGAIN`; they retry `EINTR`. Nonblocking `connect` completes only after writable notification and a successful `SO_ERROR` check. A client that closes only its write half can still receive the response. Peer close, `EPOLLERR`, malformed input, and truncated responses take explicit cleanup/error paths.

## Request lifecycle

The listener accepts a nonblocking client and creates its sole `Connection` owner. Incremental llhttp callbacks parse headers until a complete, valid GET is available. If the upstream concurrency cap is full, the reactor returns 503 without queuing. Otherwise it opens one nonblocking upstream socket, removes hop-by-hop request headers, writes `Connection: close`, and forwards the request.

The response parser requires one `Content-Length` and rejects ambiguous or unsupported framing. It buffers at most `max_response_bytes`, rebuilds a sanitized response, closes the upstream leg, and drains the client output across as many writable notifications as needed. A complete write increments `completed`; destruction unregisters fds and releases every string/parser allocation.

## Bounds, deadlines, and shutdown

`max_connections` bounds admitted client state and `max_upstream_connections` bounds in-flight upstream work. There is no waiting queue. Request and response parsers enforce configured byte limits; serialized buffers have the same bounded source data. A small overload response is written directly to a newly accepted socket and immediately closed.

All deadlines use `std::chrono::steady_clock`; timerfd wakes the reactor every 50 ms. Header parsing has an absolute deadline. Upstream connect/write/read and client response writes use an inactivity deadline refreshed by progress. SIGINT or SIGTERM closes the listener and lets active connections drain until the configured deadline. A second signal or drain expiry closes the remainder. Final stderr counters report accepted, active, completed, rejected, upstream errors, timeouts, and client write backpressure events.
