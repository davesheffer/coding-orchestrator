"""Load-tolerant timing assertions for the tests.

An absolute wall-time bound fails on a busy machine. These helpers instead check
that run time grows about linearly with input size, which still catches the
quadratic and exponential blowups the timing tests exist for, plus a generous
hang limit so a real hang fails too.

CO_STRICT_TIMING=1 also enforces each test's original absolute bound (abs_limit).
TIMING_HANG_LIMIT sets the hang limit in seconds (default 30).
"""
import contextlib
import os
import time

STRICT = os.environ.get("CO_STRICT_TIMING") == "1"
HANG_LIMIT = float(os.environ.get("TIMING_HANG_LIMIT") or 30)
# Runs faster than this are timer and scheduler noise, so ratios use it as the floor.
FLOOR = 1e-3


def _timed(fn):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def _check_hang(elapsed, label, limit=None):
    limit = HANG_LIMIT if limit is None else limit
    if elapsed >= limit:
        raise AssertionError(f"{label}: took {elapsed:.2f}s, limit {limit:g}s")


def best_of(fn, n=3, label="call"):
    """Return the shortest elapsed time (seconds) of n runs of fn()."""
    best = float("inf")
    for _ in range(n):
        elapsed, _ = _timed(fn)
        _check_hang(elapsed, label)
        best = min(best, elapsed)
    return best


def assert_scales(testcase, make_input, run, small, large, max_ratio=None, label="",
                  abs_limit=None, n=3):
    """Assert run() on make_input(large) costs at most max_ratio times make_input(small).

    max_ratio defaults to 2.5 * large / small: generous for linear growth, far
    below the square of the size ratio. Each size is timed best of up to n runs
    (stopping early once the check passes). Returns run(make_input(large)).
    """
    if max_ratio is None:
        max_ratio = 2.5 * large / small
    small_input, large_input = make_input(small), make_input(large)
    small_time = large_time = float("inf")
    result = None
    for _ in range(n):
        elapsed, _ = _timed(lambda: run(small_input))
        _check_hang(elapsed, label)
        small_time = min(small_time, elapsed)
        elapsed, result = _timed(lambda: run(large_input))
        _check_hang(elapsed, label)
        large_time = min(large_time, elapsed)
        ratio = large_time / max(small_time, FLOOR)
        if ratio <= max_ratio and not (STRICT and abs_limit is not None
                                       and large_time >= abs_limit):
            break
    times = f"{label}: n={small} took {small_time:.4f}s, n={large} took {large_time:.4f}s"
    testcase.assertLessEqual(ratio, max_ratio, f"{times}, ratio {ratio:.1f} > {max_ratio:g}")
    if STRICT and abs_limit is not None:
        testcase.assertLess(large_time, abs_limit, f"{times} (CO_STRICT_TIMING bound)")
    return result


@contextlib.contextmanager
def within(testcase, label="block", abs_limit=None, limit=None):
    """Fail if the with-block takes limit seconds (default HANG_LIMIT), or abs_limit
    when CO_STRICT_TIMING=1. Use limit for a cut-off test: the time the code would
    take if the cut-off did not work."""
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    _check_hang(elapsed, label, limit)
    if STRICT and abs_limit is not None:
        testcase.assertLess(elapsed, abs_limit, f"{label} (CO_STRICT_TIMING bound)")
