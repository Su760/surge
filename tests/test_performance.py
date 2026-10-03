"""Correctness checks only: CI never runs a performance experiment."""

import asyncio
from contextlib import redirect_stderr
import gzip
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tools import performance
from tools.backend import BoundedServer, Handler


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


class BackendTests(unittest.TestCase):
    def test_backlog_is_selected_before_listen_and_default_is_preserved(self):
        observed = []
        original = BoundedServer.server_activate

        def activate(server):
            observed.append(server.request_queue_size)
            original(server)

        with patch.object(BoundedServer, "server_activate", activate):
            for backlog in (None, 256):
                kwargs = {} if backlog is None else {"listen_backlog": backlog}
                server = BoundedServer(("127.0.0.1", 0), Handler, 2, **kwargs)
                server.server_close()
        self.assertEqual(observed, [5, 256])

    def test_direct_pairs_share_seeds_and_alternate_backlog_order(self):
        args = Mock(rates=[1600], repetitions=3, seed=20261002,
                    direct_only=True, backend_backlogs=[5, 256])
        self.assertEqual(performance.trial_plan(args), [
            ("direct", 1600, 1, 20261002, 5), ("direct", 1600, 1, 20261002, 256),
            ("direct", 1600, 2, 20261003, 256), ("direct", 1600, 2, 20261003, 5),
            ("direct", 1600, 3, 20261004, 5), ("direct", 1600, 3, 20261004, 256)])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def fake_process(self):
        return Mock(returncode=None, wait=AsyncMock(return_value=0))

    async def test_exit_race_before_term_is_reaped(self):
        process = self.fake_process()

        def vanished(sig):
            process.returncode = 0
            raise ProcessLookupError("already exited")

        process.send_signal.side_effect = vanished
        result = await performance.stop_process(process, .1, .1)
        self.assertTrue(result["graceful_exit"])
        self.assertEqual(result["exit_races"], ["term"])
        self.assertEqual(result["errors"], [])
        process.kill.assert_not_called()

    async def test_exit_race_before_kill_is_reaped(self):
        process = self.fake_process()
        process.wait.side_effect = [TimeoutError(), 0]

        def vanished():
            process.returncode = 0
            raise ProcessLookupError("already exited")

        process.kill.side_effect = vanished
        result = await performance.stop_process(process, .1, .1)
        self.assertTrue(result["graceful_exit"])
        self.assertFalse(result["forced_kill"])
        self.assertEqual(result["exit_races"], ["kill"])

    async def test_already_exited_process_receives_no_signal(self):
        process = self.fake_process()
        process.returncode = 3
        result = await performance.stop_process(process, .1, .1)
        self.assertEqual(result["status"], "already_exited")
        self.assertEqual(result["returncode"], 3)
        process.send_signal.assert_not_called()
        process.kill.assert_not_called()

    async def test_term_failure_still_attempts_kill_and_records_error(self):
        process = self.fake_process()
        process.send_signal.side_effect = PermissionError("injected TERM error")
        process.wait.side_effect = [TimeoutError(), -9]
        process.kill.side_effect = lambda: setattr(process, "returncode", -9)
        result = await performance.stop_process(process, .01, .1)
        self.assertTrue(result["forced_kill"])
        self.assertEqual(result["errors"][0]["operation"], "term")
        self.assertEqual(result["returncode"], -9)

    async def test_unreaped_process_has_a_bounded_kill_wait_and_explicit_error(self):
        process = self.fake_process()
        process.wait.side_effect = [TimeoutError(), TimeoutError()]
        result = await performance.stop_process(process, .01, .01)
        self.assertFalse(result["reaped"])
        self.assertEqual(result["errors"][0]["operation"], "kill_wait")

    async def test_real_delayed_graceful_exit_and_forced_kill(self):
        for ignore_term in (False, True):
            action = "signal.SIG_IGN" if ignore_term else "lambda *_: (time.sleep(.06), sys.exit(0))"
            code = f"import signal,time,sys; signal.signal(signal.SIGTERM, {action}); print('ready', flush=True); time.sleep(10)"
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", code,
                                                          stdout=asyncio.subprocess.PIPE)
            try:
                await asyncio.wait_for(process.stdout.readline(), 2)
                result = await performance.stop_process(process, .02 if ignore_term else .3, .3)
                self.assertTrue(result["reaped"])
                self.assertEqual(result["forced_kill"], ignore_term)
                self.assertEqual(result["graceful_exit"], not ignore_term)
                self.assertEqual(result["returncode"], -signal.SIGKILL if ignore_term else 0)
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    async def test_cleanup_errors_do_not_skip_other_children_logs_or_saving(self):
        gateway, backend = self.fake_process(), self.fake_process()
        bad_handle, good_handle = Mock(), Mock()
        bad_handle.close.side_effect = OSError("injected close failure")
        result = {"summary": {"counts": {"success": 7}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "completed.json.gz"
            with patch.object(performance, "stop_process", AsyncMock(side_effect=[
                    RuntimeError("injected cleanup failure"),
                    {"returncode": 0, "reaped": True, "errors": []}])) as stop:
                cleanup = await performance.cleanup_trial(
                    {"gateway": gateway, "backend": backend},
                    [bad_handle, good_handle], result, path, .1, .1)
            self.assertEqual(stop.await_count, 2)
            good_handle.close.assert_called_once()
            self.assertEqual(len(cleanup["log_close_errors"]), 1)
            with gzip.open(path, "rt") as saved:
                loaded = json.load(saved)
            self.assertEqual(loaded["summary"]["counts"]["success"], 7)
            self.assertIn("injected cleanup failure", loaded["cleanup"]["processes"]["gateway"]["errors"][0]["message"])

    def test_shutdown_margin_exceeds_configured_gateway_drain(self):
        self.assertEqual(performance.shutdown_timeout(5000, 2), 7)
        self.assertEqual(performance.shutdown_timeout(9000, 2), 11)
        with self.assertRaises(ValueError):
            performance.shutdown_timeout(5000, 0)

    async def test_trial_retains_distinct_raw_files_for_each_backlog(self):
        args = SimpleNamespace(backend_concurrency=64, backend_fast_ms=2,
                               handoff_budget=64, max_connections=256, max_upstreams=64,
                               gateway_drain_timeout_ms=5000, shutdown_margin_s=2, kill_wait_s=2,
                               warmup_s=1, duration_s=1, max_inflight=256, timeout_s=2)
        process = self.fake_process()
        process.pid = 123
        process.returncode = 0
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(performance.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
             patch.object(performance, "wait_port", AsyncMock()), \
             patch.object(performance, "load", AsyncMock(return_value=([record()], 1))), \
             patch.object(performance.os, "sched_getaffinity", return_value={0}, create=True), \
             patch.object(performance, "read_optional", return_value="4096"):
            for backlog in (5, 256):
                result = await performance.trial("direct", 1, 1, 10, args, Path(directory), backlog)
                self.assertEqual(result["file"], f"0001-1-direct-backlog{backlog:04d}.json.gz")
            for backlog in (5, 256):
                with gzip.open(Path(directory) / f"0001-1-direct-backlog{backlog:04d}.json.gz", "rt") as stream:
                    self.assertEqual(json.load(stream)["backend_listen_backlog"], backlog)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def argv(self, directory, *rates):
        return ["performance.py", "--output", str(directory), "--source-commit", "test",
                "--rates", *map(str, rates), "--repetitions", "1", "--direct-only"]

    async def test_populated_output_is_unchanged_and_launches_no_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for filename in ("environment.json", "index.json", ".hidden",
                             "1600-1-direct-backlog0005.json.gz",
                             "1600-1-direct-backlog0005.backend.stdout.txt"):
                (directory / filename).write_bytes(b"original evidence: " + filename.encode())
            (directory / "nested").mkdir()
            (directory / "nested" / "data").write_bytes(b"nested evidence")
            before = {str(p.relative_to(directory)): p.read_bytes()
                      for p in directory.rglob("*") if p.is_file()}
            with patch.object(sys, "argv", self.argv(directory, 1600)), \
                 patch.object(performance, "environment", return_value={}) as metadata, \
                 patch.object(performance.asyncio, "create_subprocess_exec", AsyncMock(
                     side_effect=AssertionError("child launch forbidden"))) as launch, \
                 redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    await performance.main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("nonempty", errors.getvalue())
            metadata.assert_not_called()
            launch.assert_not_awaited()
            self.assertEqual({str(p.relative_to(directory)): p.read_bytes()
                              for p in directory.rglob("*") if p.is_file()}, before)

    async def test_duplicate_rates_fail_before_output_creation_or_execution(self):
        for direct_only in (True, False):
            with self.subTest(direct_only=direct_only), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "not-created"
                argv = self.argv(directory, 1600, 1600)
                if not direct_only:
                    argv.remove("--direct-only")
                with patch.object(sys, "argv", argv), \
                     patch.object(performance, "environment", return_value={}) as metadata, \
                     patch.object(performance.asyncio, "create_subprocess_exec", AsyncMock(
                         side_effect=AssertionError("child launch forbidden"))) as launch, \
                     redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as stopped:
                        await performance.main()
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn("duplicate", errors.getvalue())
                metadata.assert_not_called()
                launch.assert_not_awaited()
                self.assertFalse(directory.exists())

    async def test_complete_plan_collision_is_rejected_even_with_distinct_seeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(sys, "argv", self.argv(directory, 1600)), \
                 patch.object(performance, "trial_plan", return_value=[
                     ("direct", 1600, 1, 10, 5), ("direct", 1600, 1, 11, 5)]), \
                 patch.object(performance, "environment", return_value={}) as metadata, \
                 patch.object(performance.asyncio, "create_subprocess_exec", AsyncMock(
                     side_effect=AssertionError("child launch forbidden"))) as launch, \
                 redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    await performance.main()
            metadata.assert_not_called()
            launch.assert_not_awaited()
            self.assertEqual(list(directory.iterdir()), [])

    async def test_raw_and_cleanup_sidecar_refuse_existing_files(self):
        for completed in (True, False):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as temporary:
                result_path = Path(temporary) / "trial.json.gz"
                existing = result_path if completed else Path(temporary) / "trial.cleanup.json"
                original = b"retained raw or cleanup evidence"
                existing.write_bytes(original)
                with self.assertRaises(FileExistsError):
                    await performance.cleanup_trial({}, [], {} if completed else None,
                                                    result_path, .1, .1)
                self.assertEqual(existing.read_bytes(), original)

    async def test_trial_log_collision_preserves_bytes_before_child_launch(self):
        args = SimpleNamespace(backend_concurrency=64, backend_fast_ms=2,
                               handoff_budget=64, max_connections=256, max_upstreams=64,
                               gateway_drain_timeout_ms=5000, shutdown_margin_s=2, kill_wait_s=2)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / "1600-1-direct-backlog0005.backend.stdout.txt"
            log.write_bytes(b"original backend log")
            with patch.object(performance.asyncio, "create_subprocess_exec", AsyncMock(
                    side_effect=AssertionError("child launch forbidden"))) as launch:
                with self.assertRaises(FileExistsError):
                    await performance.trial("direct", 1600, 1, 10, args, directory)
            launch.assert_not_awaited()
            self.assertEqual(log.read_bytes(), b"original backend log")

    async def test_metadata_claim_preserves_a_competing_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            def competing_run(args):
                (directory / "environment.json").write_bytes(b"other run claimed this directory")
                return {}
            with patch.object(sys, "argv", self.argv(directory, 1600)), \
                 patch.object(performance, "environment", side_effect=competing_run), \
                 patch.object(performance.asyncio, "create_subprocess_exec", AsyncMock(
                     side_effect=AssertionError("child launch forbidden"))) as launch:
                with self.assertRaises(FileExistsError):
                    await performance.main()
            launch.assert_not_awaited()
            self.assertEqual((directory / "environment.json").read_bytes(),
                             b"other run claimed this directory")
            self.assertEqual([p.name for p in directory.iterdir()], ["environment.json"])

    def test_failed_index_update_leaves_previous_index_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "index.json"
            original = b'[{"file":"first.json.gz"}]\n'
            path.write_bytes(original)
            with self.assertRaises(TypeError):
                performance.update_index(path, [{"file": "first.json.gz"}, {"bad": object()}])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(json.loads(path.read_text()), [{"file": "first.json.gz"}])
            self.assertEqual([p.name for p in directory.iterdir()], ["index.json"])
            performance.update_index(path, [{"file": "first.json.gz"}, {"file": "second.json.gz"}])
            self.assertEqual(json.loads(path.read_text()),
                             [{"file": "first.json.gz"}, {"file": "second.json.gz"}])

    async def test_empty_mount_and_new_directory_retain_readable_paired_trials(self):
        process = Mock(pid=123, returncode=0, wait=AsyncMock(return_value=0))
        for existing_directory in (True, False):
            with self.subTest(existing_directory=existing_directory), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) if existing_directory else Path(temporary) / "new-run"
                argv = self.argv(directory, 1) + ["--backend-backlogs", "5", "256",
                                                 "--warmup-s", "1", "--duration-s", "1"]
                with patch.object(sys, "argv", argv), \
                     patch.object(performance, "environment", return_value={"case": "empty mount"}), \
                     patch.object(performance.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                     patch.object(performance, "wait_port", AsyncMock()), \
                     patch.object(performance, "load", AsyncMock(return_value=([record()], 1))), \
                     patch.object(performance.os, "sched_getaffinity", return_value={0}, create=True), \
                     patch.object(performance, "read_optional", return_value="4096"):
                    await performance.main()
                self.assertEqual(json.loads((directory / "environment.json").read_text()),
                                 {"case": "empty mount"})
                index = json.loads((directory / "index.json").read_text())
                self.assertEqual([item["file"] for item in index], [
                    "0001-1-direct-backlog0005.json.gz", "0001-1-direct-backlog0256.json.gz"])
                for item, backlog in zip(index, (5, 256), strict=True):
                    with gzip.open(directory / item["file"], "rt") as stream:
                        raw = json.load(stream)
                    self.assertEqual(raw["backend_listen_backlog"], backlog)
                    self.assertEqual(raw["records"], [record()])
                    for log in item["logs"].values():
                        self.assertTrue((directory / log).is_file())
                self.assertFalse((directory / ".index.json.tmp").exists())


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
