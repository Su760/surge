#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from contextlib import closing


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port: int, process: subprocess.Popen, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            out, err = process.communicate()
            raise RuntimeError(f"process exited {process.returncode}\nstdout={out}\nstderr={err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError(f"port {port} did not become ready")


def receive_all(sock: socket.socket, chunk_size: int = 65536, pause: float = 0) -> bytes:
    chunks = []
    while True:
        data = sock.recv(chunk_size)
        if not data:
            return b"".join(chunks)
        chunks.append(data)
        if pause:
            time.sleep(pause)


def split_response(response: bytes) -> tuple[int, dict[bytes, bytes], bytes]:
    head, body = response.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers[name.lower()] = value.strip()
    return status, headers, body


class Process:
    def __init__(self, command: list[str]):
        self.command = command
        self.process: subprocess.Popen | None = None

    def start(self) -> "Process":
        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return self

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 7) -> tuple[str, str]:
        assert self.process is not None
        if self.process.poll() is None:
            self.process.send_signal(sig)
        try:
            return self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            out, err = self.process.communicate()
            raise AssertionError(f"process failed to stop: {self.command}\n{out}\n{err}")


class Harness:
    gateway_binary = ""
    backend_script = ""
    default_workers = 1

    def __init__(
        self,
        *,
        gateway_options: list[str] | None = None,
        backend: bool = True,
        workers: int | None = None,
    ):
        self.gateway_port = free_port()
        self.backend_port = free_port()
        self.backend_process: Process | None = None
        self.gateway_process: Process | None = None
        self.gateway_options = gateway_options or []
        self.with_backend = backend
        self.workers = workers if workers is not None else self.default_workers

    def __enter__(self) -> "Harness":
        if self.with_backend:
            self.backend_process = Process(
                [sys.executable, self.backend_script, "--port", str(self.backend_port),
                 "--max-concurrency", "8"]
            ).start()
            wait_for_port(self.backend_port, self.backend_process.process)
        command = [
            self.gateway_binary,
            "--listen-address", "127.0.0.1",
            "--listen-port", str(self.gateway_port),
            "--upstream", f"127.0.0.1:{self.backend_port}",
            "--workers", str(self.workers),
            "--stats-interval-ms", "0",
            *self.gateway_options,
        ]
        self.gateway_process = Process(command).start()
        wait_for_port(self.gateway_port, self.gateway_process.process)
        time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.gateway_process:
            self.gateway_process.stop()
        if self.backend_process:
            self.backend_process.stop()

    def stop_gateway(self, sig: int = signal.SIGTERM, timeout: float = 7) -> tuple[str, str]:
        assert self.gateway_process is not None
        output = self.gateway_process.stop(sig=sig, timeout=timeout)
        self.gateway_process = None
        return output

    def connect(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.gateway_port), timeout=4)
        sock.settimeout(4)
        return sock

    def request(self, target: str = "/fast") -> tuple[int, dict[bytes, bytes], bytes]:
        with self.connect() as sock:
            sock.sendall(f"GET {target} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
            return split_response(receive_all(sock))


class GatewayIntegrationTests(unittest.TestCase):
    def test_forwarding_fragmented_headers_and_fragmented_response(self):
        with Harness() as harness:
            with harness.connect() as sock:
                for fragment in [b"GET /fragment HTTP/1.1\r\nHo", b"st: test\r\nX-Test: yes\r\n", b"\r\n"]:
                    sock.sendall(fragment)
                    time.sleep(0.01)
                status, headers, body = split_response(receive_all(sock))
            self.assertEqual(status, 200)
            self.assertEqual(body, b"fragmented\n")
            self.assertEqual(headers[b"content-length"], b"11")
            self.assertEqual(headers[b"connection"], b"close")
            self.assertEqual(headers[b"x-backend"], b"fragmented")

    def test_multiple_concurrent_fast_and_slow_requests(self):
        with Harness() as harness:
            results = {}
            def call(name: str, path: str) -> None:
                results[name] = harness.request(path)
            slow = threading.Thread(target=call, args=("slow", "/slow"))
            fast = threading.Thread(target=call, args=("fast", "/fast"))
            slow.start()
            time.sleep(0.03)
            fast.start()
            slow.join()
            fast.join()
            self.assertEqual(results["fast"][2], b"fast\n")
            self.assertEqual(results["slow"][2], b"slow\n")

    def test_slow_reader_receives_complete_large_response(self):
        harness = Harness(gateway_options=["--max-response-bytes", str(17 * 1024 * 1024)])
        harness.__enter__()
        try:
            with harness.connect() as sock:
                sock.sendall(b"GET /large?bytes=16777216 HTTP/1.1\r\nHost: test\r\n\r\n")
                time.sleep(0.5)
                response = receive_all(sock)
            status, headers, body = split_response(response)
            self.assertEqual(status, 200)
            self.assertEqual(int(headers[b"content-length"]), 16 * 1024 * 1024)
            self.assertEqual(len(body), 16 * 1024 * 1024)
            self.assertEqual(body[:1] + body[-1:], b"xx")
            _, err = harness.stop_gateway()
            match = re.search(r"client_backpressure_events=(\d+)", err)
            self.assertIsNotNone(match, err)
            self.assertGreater(int(match.group(1)), 0, err)
        finally:
            harness.__exit__(None, None, None)

    def test_client_half_close_still_receives_response(self):
        with Harness() as harness:
            with harness.connect() as sock:
                sock.sendall(
                    b"GET /slow?seconds=0.2 HTTP/1.1\r\nHost: test\r\n\r\n"
                )
                sock.shutdown(socket.SHUT_WR)
                status, _, body = split_response(receive_all(sock))
            self.assertEqual(status, 200)
            self.assertEqual(body, b"slow\n")

    def test_upstream_connect_failure_and_timeout(self):
        with Harness(backend=False) as harness:
            self.assertEqual(harness.request()[0], 502)
        with Harness(gateway_options=["--upstream-timeout-ms", "100"]) as harness:
            status, _, _ = harness.request("/hang?seconds=1")
            self.assertEqual(status, 504)

    def test_client_disconnect_does_not_poison_reactor(self):
        with Harness() as harness:
            sock = harness.connect()
            sock.sendall(b"GET /slow HTTP/1.1\r\nHost: test\r\n\r\n")
            sock.close()
            time.sleep(0.05)
            self.assertEqual(harness.request("/fast")[2], b"fast\n")

    def test_closed_connections_do_not_poison_reused_descriptors(self):
        with Harness() as harness:
            for _ in range(100):
                abandoned = harness.connect()
                abandoned.sendall(b"GET /close HTTP/1.1\r\nHost: test\r\n\r\n")
                abandoned.close()
                status, _, body = harness.request("/fast")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"fast\n")

    def test_multiple_workers_process_connections(self):
        harness = Harness(workers=4)
        harness.__enter__()
        try:
            for _ in range(12):
                self.assertEqual(harness.request("/fast")[2], b"fast\n")
            _, err = harness.stop_gateway()
            match = re.search(r"worker_connections=([^\n]+)", err)
            self.assertIsNotNone(match, err)
            counts = [int(item.split(":", 1)[1]) for item in match.group(1).split(",")]
            self.assertEqual(len(counts), 4, err)
            self.assertGreaterEqual(sum(value > 0 for value in counts), 2, err)
        finally:
            harness.__exit__(None, None, None)

    def test_malformed_and_unsupported_requests(self):
        wires = [
            b"garbage\r\n\r\n",
            b"POST /fast HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\n\r\n",
            b"GET /fast HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            b"GET /fast HTTP/1.1\r\nHost: one\r\nHost: two\r\n\r\n",
        ]
        with Harness() as harness:
            for wire in wires:
                with self.subTest(wire=wire), harness.connect() as sock:
                    sock.sendall(wire)
                    status, _, _ = split_response(receive_all(sock))
                    self.assertEqual(status, 400)

    def test_connection_upstream_and_buffer_limits(self):
        with Harness(gateway_options=["--max-connections", "1"]) as harness:
            held = harness.connect()
            held.sendall(b"GET /fast HTTP/1.1\r\nHost: test\r\nX-Hold: ")
            with harness.connect() as rejected:
                rejected.sendall(b"GET /fast HTTP/1.1\r\nHost: test\r\n\r\n")
                self.assertEqual(split_response(receive_all(rejected))[0], 503)
            held.close()

        with Harness(gateway_options=["--max-upstream-connections", "1"]) as harness:
            result = {}
            slow = threading.Thread(target=lambda: result.setdefault("slow", harness.request("/slow")))
            slow.start()
            time.sleep(0.05)
            self.assertEqual(harness.request("/fast")[0], 503)
            slow.join()
            self.assertEqual(result["slow"][0], 200)

        with Harness(gateway_options=["--max-request-bytes", "128"]) as harness:
            with harness.connect() as sock:
                sock.sendall(b"GET /fast HTTP/1.1\r\nHost: test\r\nX-Large: " + b"a" * 256 + b"\r\n\r\n")
                self.assertEqual(split_response(receive_all(sock))[0], 431)

        with Harness(gateway_options=["--max-response-bytes", "1024"]) as harness:
            self.assertEqual(harness.request("/large?bytes=2048")[0], 502)

    def test_sigterm_drains_active_request_and_reports_counters(self):
        harness = Harness(gateway_options=["--drain-timeout-ms", "1000"])
        harness.__enter__()
        try:
            result = {}
            request = threading.Thread(target=lambda: result.setdefault("response", harness.request("/slow")))
            request.start()
            time.sleep(0.05)
            harness.gateway_process.process.send_signal(signal.SIGTERM)
            request.join(timeout=2)
            self.assertFalse(request.is_alive())
            self.assertEqual(result["response"][0], 200)
            out, err = harness.gateway_process.process.communicate(timeout=2)
            self.assertEqual(harness.gateway_process.process.returncode, 0, (out, err))
            self.assertIn("stats accepted=", err)
            harness.gateway_process = None
        finally:
            harness.__exit__(None, None, None)

    def test_sigterm_bounds_stalled_upstream_and_client(self):
        upstream = Harness(gateway_options=["--drain-timeout-ms", "150"])
        upstream.__enter__()
        upstream_client = upstream.connect()
        try:
            upstream_client.sendall(
                b"GET /hang?seconds=5 HTTP/1.1\r\nHost: test\r\n\r\n"
            )
            time.sleep(0.1)
            started = time.monotonic()
            out, err = upstream.stop_gateway(timeout=2)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIn("active=0", err, (out, err))
        finally:
            upstream_client.close()
            upstream.__exit__(None, None, None)

        client = Harness(
            gateway_options=[
                "--drain-timeout-ms", "150",
                "--max-response-bytes", str(17 * 1024 * 1024),
            ]
        )
        client.__enter__()
        slow_client = client.connect()
        try:
            slow_client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            slow_client.sendall(
                b"GET /large?bytes=16777216 HTTP/1.1\r\nHost: test\r\n\r\n"
            )
            time.sleep(0.5)
            started = time.monotonic()
            out, err = client.stop_gateway(timeout=2)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIn("active=0", err, (out, err))
            match = re.search(r"client_backpressure_events=(\d+)", err)
            self.assertIsNotNone(match, err)
            self.assertGreater(int(match.group(1)), 0, err)
        finally:
            slow_client.close()
            client.__exit__(None, None, None)

    def test_shutdown_cleans_many_queued_or_active_connections(self):
        harness = Harness(
            workers=4,
            gateway_options=[
                "--handoff-queue-capacity", "1",
                "--max-connections", "128",
                "--drain-timeout-ms", "150",
            ],
        )
        harness.__enter__()
        clients = []
        try:
            for _ in range(64):
                try:
                    sock = harness.connect()
                    sock.sendall(b"GET /slow HTTP/1.1\r\nHost: test\r\nX-Hold: ")
                    clients.append(sock)
                except OSError:
                    pass
            started = time.monotonic()
            out, err = harness.stop_gateway(timeout=2)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIn("active=0", err, (out, err))
            self.assertIn("queued=0", err, (out, err))
        finally:
            for sock in clients:
                sock.close()
            harness.__exit__(None, None, None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--workers", type=int, default=1)
    args, remaining = parser.parse_known_args()
    Harness.gateway_binary = os.path.abspath(args.gateway)
    Harness.backend_script = os.path.abspath(args.backend)
    Harness.default_workers = args.workers
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)


if __name__ == "__main__":
    main()
