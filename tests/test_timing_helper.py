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


def quadratic(items):
    total = 0
    for item in items:
        for other in items:
            total += item ^ other
    return total


class TimingHelperTests(unittest.TestCase):
    def test_linear_work_passes(self):
        result = timing.assert_scales(self, lambda n: list(range(n)), linear,
                                      50_000, 200_000, label="linear")
        self.assertEqual(result, sum(range(200_000)))

    def test_quadratic_work_fails(self):
        # 4x the input is 16x the work, well past the default 10x allowance.
        with self.assertRaises(AssertionError) as raised:
            timing.assert_scales(self, lambda n: list(range(n)), quadratic, 300, 1200,
                                 label="quadratic")
        self.assertIn("ratio", str(raised.exception))

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
