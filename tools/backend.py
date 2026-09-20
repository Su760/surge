#!/usr/bin/env python3
"""Controlled HTTP/1.1 backend for Surge integration tests and demos.

Delays use sleep and model service latency, not CPU work. The server bounds
active request handlers but is not a load generator or performance oracle.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, max_concurrency: int):
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(max_concurrency)

    def process_request(self, request: socket.socket, client_address) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                body = b"backend busy\n"
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + body
                )
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SurgeBackend/1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/close":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if parsed.path == "/hang":
            time.sleep(float(query.get("seconds", [self.server.hang_seconds])[0]))
            self._send(b"late\n")
            return
        if parsed.path == "/slow":
            time.sleep(float(query.get("seconds", [self.server.slow_seconds])[0]))
            self._send(b"slow\n")
            return
        if parsed.path == "/large":
            size = int(query.get("bytes", [str(4 * 1024 * 1024)])[0])
            self._send(b"x" * size)
            return
        if parsed.path == "/fragment":
            body = b"fragmented\n"
            pieces = [
                b"HTTP/1.1 200 OK\r\nContent-Len",
                f"gth: {len(body)}\r\nX-Backend: fragmented\r\n\r\n".encode(),
                body[:3],
                body[3:],
            ]
            for piece in pieces:
                self.connection.sendall(piece)
                time.sleep(self.server.fragment_delay_seconds)
            self.close_connection = True
            return
        if parsed.path == "/fast":
            time.sleep(self.server.fast_seconds)
            self._send(b"fast\n")
            return
        self._send(b"missing\n", status="404 Not Found")

    def _send(self, body: bytes, status: str = "200 OK") -> None:
        self.send_response_only(int(status.split()[0]), " ".join(status.split()[1:]))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "text/plain")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument("--fast-ms", type=float, default=0)
    parser.add_argument("--slow-ms", type=float, default=250)
    parser.add_argument("--hang-ms", type=float, default=5000)
    parser.add_argument("--fragment-ms", type=float, default=10)
    args = parser.parse_args()
    server = BoundedServer((args.host, args.port), Handler, args.max_concurrency)
    server.fast_seconds = args.fast_ms / 1000
    server.slow_seconds = args.slow_ms / 1000
    server.hang_seconds = args.hang_ms / 1000
    server.fragment_delay_seconds = args.fragment_ms / 1000
    print(f"backend listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
