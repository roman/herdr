import unittest

from scripts import api_perf


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self):
        values = [5, 1, 4, 2, 3]
        self.assertEqual(api_perf.percentile(values, 0.5), 3)
        self.assertEqual(api_perf.percentile(values, 0.95), 5)

    def test_empty_samples_have_no_percentile(self):
        self.assertIsNone(api_perf.percentile([], 0.5))


class ParsePsTimeTest(unittest.TestCase):
    def test_macos_minutes_and_hundredths(self):
        self.assertAlmostEqual(api_perf.parse_ps_time(" 1:02.50\n"), 62.5)


class FakeEvents:
    def __init__(self, *events):
        self.events = list(events)

    def next(self):
        return self.events.pop(0)


class ExpectRenameTest(unittest.TestCase):
    def test_accepts_the_rename_just_sent(self):
        events = FakeEvents({"data": {"workspace": {"label": "burst-3"}}})
        api_perf.expect_rename(events, "burst-3")

    def test_rejects_an_event_for_another_rename(self):
        events = FakeEvents({"data": {"workspace": {"label": "burst-2"}}})
        with self.assertRaises(api_perf.ApiError):
            api_perf.expect_rename(events, "burst-3")


class CompareTest(unittest.TestCase):
    def test_reports_median_change_against_baseline(self):
        results = [
            {"label": "baseline", "ping_p50_ms": 1.0},
            {"label": "baseline", "ping_p50_ms": 3.0},
            {"label": "candidate", "ping_p50_ms": 1.0},
        ]
        line = next(line for line in api_perf.compare(results)
                    if line.startswith("ping_p50_ms"))
        self.assertEqual(line.split()[1:], ["2.000", "1.000", "-50.0%"])

    def test_missing_metric_prints_dashes(self):
        results = [{"label": "baseline"}, {"label": "candidate"}]
        line = next(line for line in api_perf.compare(results)
                    if line.startswith("ping_syscalls_per_request"))
        self.assertEqual(line.split()[1:], ["-", "-", "-"])


if __name__ == "__main__":
    unittest.main()
