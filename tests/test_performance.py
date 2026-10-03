"""Correctness checks only: CI never runs a performance experiment."""

import asyncio
import time
import unittest
from unittest.mock import patch

from tools import performance


def record(arrival_id=0, outcome="success", scheduled=0, dispatched=2,
           complete=12, connected=4, header=10, body=12):
    return {"arrival_id": arrival_id, "scheduled_ms": scheduled,
            "dispatched_ms": dispatched, "complete_ms": complete,
            "connected_ms": connected, "header_ms": header, "body_complete_ms": body,
            "outcome": outcome, "http_status": 200 if outcome == "success" else None}


class MetricsTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self):
        self.assertIsNone(performance.percentile([], 95))
        self.assertEqual(performance.percentile([10, 20, 30, 40, 50], 50), 30)
        self.assertEqual(performance.percentile([10, 20, 30, 40, 50], 95), 48)

    def test_every_scheduled_arrival_has_one_outcome(self):
        records = [record(), record(1, "http_rejection", 10, 11, 16),
                   record(2, "timeout", 20, 22, 30),
                   record(3, "generator_drop", 30, None, None, None, None, None)]
        result = performance.summarize(records, .04, .05, 100)
        self.assertEqual(result["scheduled"], 4)
        self.assertEqual(result["dispatched"], 3)
        self.assertEqual(sum(result["counts"].values()), 4)
        self.assertEqual(result["success_per_s_including_drain"], 20)
        self.assertEqual(result["eventual_success_fraction"], .25)
        self.assertEqual(result["rejection_rate"], .25)
        self.assertEqual(result["error_rate"], .25)
        self.assertEqual(result["response_latency"]["p50_ms"], 10)
        self.assertEqual(result["scheduled_arrival_latency"]["p50_ms"], 12)
        self.assertTrue(result["generator_limited"])

    def test_missing_outcome_is_detected_from_offered_schedule(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            performance.summarize([record(0)], 1, 1, 2)

    def test_duplicate_cannot_replace_missing_outcome(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            performance.summarize([record(0), record(0)], 1, 1, 2)

    def test_extra_duplicate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            performance.summarize([record(0), record(1), record(1)], 1, 1, 2)

    def test_unexpected_id_cannot_replace_missing_outcome(self):
        with self.assertRaisesRegex(ValueError, "unexpected"):
            performance.summarize([record(0), record(2)], 1, 1, 2)

    def test_unknown_outcome_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outcome"):
            performance.summarize([record(outcome="unknown")], 1, 1, 1)

    def test_missing_id_and_dispatch_state_mismatch_are_rejected(self):
        missing_id = record()
        del missing_id["arrival_id"]
        with self.assertRaisesRegex(ValueError, "arrival_id"):
            performance.summarize([missing_id], 1, 1, 1)
        with self.assertRaisesRegex(ValueError, "dispatch state"):
            performance.summarize([record(dispatched=None)], 1, 1, 1)

    def test_expected_count_uses_full_offered_intervals(self):
        self.assertEqual(performance.expected_arrivals(3, .9), 2)
        self.assertEqual(performance.expected_arrivals(1600, 30), 48000)
        self.assertEqual(performance.expected_arrivals(2400, 30), 72000)
        self.assertEqual(performance.summarize([], 0, 0, 2400)["scheduled"], 0)

    def test_measured_successes_exclude_post_window_completions(self):
        records = [record(0, complete=999, body=999),
                   record(1, complete=1000, body=1000),
                   record(2, complete=1200, body=1200),
                   record(3, "http_rejection", complete=30)]
        result = performance.summarize(records, 1, 1.5, 4)
        self.assertEqual(result["successes_in_measured_window"], 1)
        self.assertEqual(result["success_per_s_measured_window"], 1)
        self.assertEqual(result["counts"]["success"], 3)
        self.assertEqual(result["eventual_success_fraction"], .75)
        self.assertEqual(result["success_per_s_including_drain"], 2)
        self.assertEqual(result["drain_duration_s"], .5)
        self.assertEqual(result["response_latency"]["max_ms"], 1198)
        self.assertEqual(result["response_latency"]["over_1000ms"], 1)
        self.assertEqual(result["counts"]["http_rejection"], 1)

    def test_phase_metrics_and_failed_phase_progress(self):
        records = [record(0, complete=1510, connected=1202, header=1502, body=1510),
                   record(1, "timeout", complete=2002, connected=5, header=None, body=None)]
        result = performance.summarize(records, 1, 2.1, 2)
        self.assertEqual(result["success_phases"]["tcp_connect"]["max_ms"], 1200)
        self.assertEqual(result["success_phases"]["connect_to_header"]["max_ms"], 300)
        self.assertEqual(result["success_phases"]["header_to_body"]["max_ms"], 8)
        self.assertEqual(result["phase_progress"]["timeout"],
                         {"connected": 1, "header_received": 0, "body_completed": 0})
        self.assertEqual(result["all_dispatched_latency"]["over_1000ms"], 2)

    def test_dispatch_lag_alone_marks_generator_limited(self):
        self.assertTrue(performance.summarize([record(dispatched=12, complete=15)],
                                             1, 1, 1)["generator_limited"])

    def test_resource_windows_do_not_dilute_measurement_with_drain(self):
        measured = performance.resource_summary({"backend": [(10, 100), (12, 200)]}, 1)
        drained = performance.resource_summary({"backend": [(12, 200), (12.1, 900)]}, 2)
        self.assertEqual(measured["backend"]["cpu_percent_one_core"], 200)
        self.assertEqual(measured["backend"]["peak_rss_bytes"], 200)
        self.assertAlmostEqual(drained["backend"]["cpu_percent_one_core"], 5)
        self.assertEqual(drained["backend"]["peak_rss_bytes"], 900)

    def test_kernel_counter_parser_preserves_available_fields(self):
        result = performance.parse_tcp_counters(
            "TcpExt: ListenDrops TCPSynRetrans ListenOverflows\nTcpExt: 9 3 7\n", "TcpExt")
        self.assertEqual(result, {"ListenDrops": 9, "TCPSynRetrans": 3, "ListenOverflows": 7})
        self.assertEqual(performance.counter_delta({"x": 1}, {"x": 4, "y": 2}), {"x": 3})

    def test_queue_budget_is_constant_for_one_and_four_workers(self):
        self.assertEqual(performance.per_worker_capacity(64, 1), 64)
        self.assertEqual(performance.per_worker_capacity(64, 4), 16)
        with self.assertRaises(ValueError):
            performance.per_worker_capacity(65, 4)

    def test_configuration_order_alternates_and_balances_positions(self):
        orders = [performance.configuration_order(index) for index in range(6)]
        self.assertEqual(orders[0], ["direct", "one", "four"])
        self.assertEqual(orders[1], ["four", "one", "direct"])
        self.assertEqual(len({tuple(order) for order in orders}), 6)
        for position in range(3):
            for config in ("direct", "one", "four"):
                self.assertEqual(sum(order[position] == config for order in orders), 2)


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, response, timeout=.5):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                for delay, data in response:
                    await asyncio.sleep(delay)
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        async with server:
            return await performance.request_once(server.sockets[0].getsockname()[1],
                                                  7, 0, time.perf_counter(), timeout)

    async def test_actual_header_and_body_receive_times_are_distinct(self):
        result = await self.exchange([(0, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n"),
                                      (.02, b"ok")])
        self.assertEqual(result["arrival_id"], 7)
        self.assertEqual(result["outcome"], "success")
        self.assertLessEqual(result["dispatched_ms"], result["connected_ms"])
        self.assertLessEqual(result["connected_ms"], result["header_ms"])
        self.assertGreaterEqual(result["body_complete_ms"] - result["header_ms"], 10)
        self.assertLessEqual(result["body_complete_ms"], result["complete_ms"])

    async def test_peer_close_preserves_header_but_has_no_body_completion(self):
        result = await self.exchange([(0, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n")])
        self.assertEqual(result["outcome"], "protocol_error")
        self.assertIsNotNone(result["header_ms"])
        self.assertIsNone(result["body_complete_ms"])

    async def test_timeout_preserves_received_phase_timestamps(self):
        result = await self.exchange([(0, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n"),
                                      (.05, b"ok")], timeout=.01)
        self.assertEqual(result["outcome"], "timeout")
        self.assertIsNotNone(result["connected_ms"])
        self.assertIsNotNone(result["header_ms"])
        self.assertIsNone(result["body_complete_ms"])

    async def test_missing_length_is_not_a_success(self):
        result = await self.exchange([(0, b"HTTP/1.1 200 OK\r\n\r\n")])
        self.assertEqual(result["outcome"], "protocol_error")

    async def test_load_assigns_ids_to_successes_and_bounded_drops(self):
        async def slow_request(port, arrival_id, scheduled_ms, origin, timeout_s):
            dispatched = (time.perf_counter() - origin) * 1000
            await asyncio.sleep(.05)
            return record(arrival_id, scheduled=scheduled_ms, dispatched=dispatched,
                          complete=(time.perf_counter() - origin) * 1000)
        with patch.object(performance, "request_once", slow_request):
            records, elapsed = await performance.load(1, 200, .025, 10, 1, 1)
        result = performance.summarize(records, .025, elapsed, 200)
        self.assertEqual(sorted(r["arrival_id"] for r in records), [0, 1, 2, 3, 4])
        self.assertEqual(result["counts"]["success"], 1)
        self.assertEqual(result["counts"]["generator_drop"], 4)

    async def test_internal_task_exception_invalidates_trial(self):
        async def broken_request(*args):
            raise RuntimeError("injected task failure")
        with patch.object(performance, "request_once", broken_request):
            with self.assertRaisesRegex(RuntimeError, "injected task failure"):
                await performance.load(1, 100, .01, 10, 1, 1)

    async def test_load_samples_drain_memory_in_a_separate_window(self):
        start = time.perf_counter()
        snapshots = 0

        def kernel_snapshot():
            nonlocal snapshots
            snapshots += 1
            return {"tcp": {}, "cgroup_cpu": {}}

        def usage(pid):
            elapsed = time.perf_counter() - start
            return elapsed, 100 if snapshots < 2 else 900

        async def slow_request(port, arrival_id, scheduled_ms, origin, timeout_s):
            await asyncio.sleep(.06)
            return record(arrival_id, scheduled=scheduled_ms,
                          complete=(time.perf_counter() - origin) * 1000)

        diagnostics = {}
        with patch.object(performance, "request_once", slow_request), \
             patch.object(performance, "process_usage", usage), \
             patch.object(performance, "kernel_snapshot", kernel_snapshot):
            await performance.load(1, 50, .02, 10, 1, 1, {"backend": 1}, diagnostics)
        self.assertEqual(diagnostics["resources"]["measurement"]["backend"]["peak_rss_bytes"], 100)
        self.assertEqual(diagnostics["resources"]["drain"]["backend"]["peak_rss_bytes"], 900)


if __name__ == "__main__":
    unittest.main()
