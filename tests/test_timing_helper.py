import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timing  # noqa: E402


def linear(items):
    total = 0
    for item in items:
        total += item
    return total


class TimingHelperTests(unittest.TestCase):
    def test_linear_work_passes(self):
        result = timing.assert_scales(self, lambda n: list(range(n)), linear,
                                      50_000, 200_000, label="linear")
        self.assertEqual(result, sum(range(200_000)))

    def test_quadratic_work_fails(self):
        # 4x the input is 16x the work, well past the default 10x allowance. A fake clock
        # advanced by each run makes it cost exactly n**2: with real timing, a stalled first
        # small-size run on a busy runner could bring the ratio under 10x, and assert_scales
        # stops at the first passing pair, so the expected failure did not happen.
        clock = [0.0]

        def run(n):
            clock[0] += n * n * 1e-7
            return n

        with mock.patch.object(timing.time, "perf_counter", lambda: clock[0]), \
                self.assertRaises(AssertionError) as raised:
            timing.assert_scales(self, lambda n: n, run, 300, 1200, label="quadratic")
        self.assertIn("ratio", str(raised.exception))

    def scripted(self, costs):
        # A fake clock: run call i costs costs[i] seconds and returns i.
        clock = [0.0]
        calls = iter(range(len(costs)))

        def run(n):
            i = next(calls)
            clock[0] += costs[i]
            return i
        return clock, run

    def test_scales_keeps_each_sizes_fastest_run(self):
        # Rounds of (small, large): each size's stalled run must be discarded for its
        # best-so-far. Min/min ratios are 10, 4 and 1.5, so the third round passes. Small's
        # last run (0.5 > 0.1) passes round two; small's never-lowered max passes round one;
        # large's never-lowered max never passes.
        clock, run = self.scripted([0.1, 1.0, 0.5, 0.4, 0.3, 0.15])
        with mock.patch.object(timing.time, "perf_counter", lambda: clock[0]):
            result = timing.assert_scales(self, lambda n: n, run, 1, 2, max_ratio=2.0,
                                          label="scripted", n=3)
        self.assertEqual(result, 5)

    def test_scales_reports_each_sizes_fastest_run(self):
        # Every round fails; the report must show each size's minimum (0.1 and 0.6), not
        # its last run (0.2 and 0.8).
        clock, run = self.scripted([0.3, 0.9, 0.1, 0.6, 0.2, 0.8])
        with mock.patch.object(timing.time, "perf_counter", lambda: clock[0]), \
                self.assertRaises(AssertionError) as raised:
            timing.assert_scales(self, lambda n: n, run, 1, 2, max_ratio=2.0, label="scripted",
                                 n=3)
        self.assertIn("n=1 took 0.1000s, n=2 took 0.6000s, ratio 6.0 > 2", str(raised.exception))

    def test_best_of_returns_shortest_run(self):
        times = iter([0.0, 5.0, 10.0, 11.0, 20.0, 20.5])
        with mock.patch.object(timing.time, "perf_counter", lambda: next(times)):
            self.assertEqual(timing.best_of(lambda: None), 0.5)

    def test_hang_limit_fails(self):
        with mock.patch.object(timing, "HANG_LIMIT", 0.0):
            with self.assertRaises(AssertionError):
                timing.best_of(lambda: None, n=1)
            with self.assertRaises(AssertionError):
                with timing.within(self, "hang"):
                    pass

    def test_strict_enforces_absolute_limit(self):
        with mock.patch.object(timing, "STRICT", True):
            with self.assertRaises(AssertionError) as raised:
                timing.assert_scales(self, lambda n: list(range(n)), linear, 1000, 4000,
                                     abs_limit=0.0, label="strict")
            self.assertIn("CO_STRICT_TIMING", str(raised.exception))
        with mock.patch.object(timing, "STRICT", False):
            timing.assert_scales(self, lambda n: list(range(n)), linear, 1000, 4000,
                                 abs_limit=0.0, label="lenient")


if __name__ == "__main__":
    unittest.main()
