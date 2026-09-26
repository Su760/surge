"""Focused checks for benchmark outcome and latency accounting."""

import unittest

from tools.performance import percentile, summarize


class MetricsTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self):
        self.assertIsNone(percentile([], 95))
        self.assertEqual(percentile([10, 20, 30, 40, 50], 50), 30)
        self.assertEqual(percentile([10, 20, 30, 40, 50], 95), 48)

    def test_every_scheduled_arrival_has_one_outcome(self):
        records = [
            {"scheduled_ms": 0, "dispatched_ms": 2, "complete_ms": 12, "outcome": "success"},
            {"scheduled_ms": 10, "dispatched_ms": 11, "complete_ms": 16, "outcome": "http_rejection"},
            {"scheduled_ms": 20, "dispatched_ms": 22, "complete_ms": 30, "outcome": "timeout"},
            {"scheduled_ms": 30, "dispatched_ms": None, "complete_ms": None, "outcome": "generator_drop"},
        ]
        result = summarize(records, 0.04, 0.05)
        self.assertEqual(result["scheduled"], 4)
        self.assertEqual(result["dispatched"], 3)
        self.assertEqual(sum(result["counts"].values()), 4)
        self.assertEqual(result["success_per_s"], 20)
        self.assertEqual(result["success_rate"], 0.25)
        self.assertEqual(result["rejection_rate"], 0.25)
        self.assertEqual(result["error_rate"], 0.25)
        self.assertEqual(result["response_latency"]["p50_ms"], 10)
        self.assertEqual(result["scheduled_arrival_latency"]["p50_ms"], 12)
        self.assertTrue(result["generator_limited"])

    def test_dispatch_lag_alone_marks_generator_limited(self):
        records = [{"scheduled_ms": 0, "dispatched_ms": 12, "complete_ms": 15,
                    "outcome": "success"}]
        self.assertTrue(summarize(records, 1, 1)["generator_limited"])


if __name__ == "__main__":
    unittest.main()
