"""Critic cases for the effort benchmark: old/new file text, the change's stated intent, and the seeded bug line.

Seven cases carry a seeded defect and one (`money`) is clean (`seed=None`). `seed` is a substring that occurs
exactly once in `new`; its line is the seeded line. `retry` inserts `max_delay` before `sleep`, which breaks
positional callers such as `retry(fn, 3, 0.5, my_sleep)`.
A case may set `accept=(lo, hi)`: the inclusive range of new-file lines that count as a hit (default seeded
line +/- 2). `lockfile` accepts 9-12, the exists-check through the non-exclusive os.open.
"""

CASES = {}

CASES["ratelimit"] = dict(file="ratelimit.py", kind="off-by-one boundary",
    intent="Allow up to `limit` calls per `window` seconds; refactor to a deque.",
    old='''import time


class RateLimiter:
    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.calls = []

    def allow(self):
        now = self.clock()
        self.calls = [t for t in self.calls if now - t < self.window]
        if len(self.calls) < self.limit:
            self.calls.append(now)
            return True
        return False
''',
    new='''import time
from collections import deque


class RateLimiter:
    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.calls = deque()

    def allow(self):
        now = self.clock()
        while self.calls and now - self.calls[0] >= self.window:
            self.calls.popleft()
        if len(self.calls) <= self.limit:
            self.calls.append(now)
            return True
        return False
''', seed="if len(self.calls) <= self.limit:")

CASES["config"] = dict(file="config.py", kind="swallowed exception",
    intent="Support an optional `defaults` dict merged under the file's values.",
    old='''import json


class ConfigError(Exception):
    pass


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot load {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")
    return data
''',
    new='''import json


class ConfigError(Exception):
    pass


def load_config(path, defaults=None):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")
    merged = dict(defaults or {})
    merged.update(data)
    return merged
''', seed="        data = {}")

CASES["static"] = dict(file="static.py", kind="path traversal",
    intent="Simplify path handling with pathlib; behaviour unchanged.",
    old='''import os


def resolve(root, name):
    """Map a request path under root to a file path; refuse anything outside root."""
    root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, name.lstrip("/")))
    if os.path.commonpath([root, target]) != root:
        raise PermissionError(name)
    return target
''',
    new='''from pathlib import Path


def resolve(root, name):
    """Map a request path under root to a file path; refuse anything outside root."""
    root = Path(root).resolve()
    target = root / name.lstrip("/")
    if not str(target).startswith(str(root)):
        raise PermissionError(name)
    return str(target)
''', seed='target = root / name.lstrip("/")')

CASES["lockfile"] = dict(file="lockfile.py", kind="TOCTOU race",
    intent="Give a clearer error message when the lock is already held.",
    old='''import os


class LockHeld(Exception):
    pass


def acquire(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise LockHeld(path) from None
    os.write(fd, str(os.getpid()).encode())
    return fd
''',
    new='''import os


class LockHeld(Exception):
    pass


def acquire(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raise LockHeld(f"{path} is held by pid {f.read().strip()}")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    return fd
''', seed="fd = os.open(path, os.O_CREAT | os.O_WRONLY)", accept=(9, 12))

CASES["topn"] = dict(file="report.py", kind="input mutation",
    intent="Speed up top_n by avoiding a copy of the list.",
    old='''def top_n(scores, n):
    """Return the n highest (name, score) pairs, highest first. `scores` is not modified."""
    if n < 0:
        raise ValueError("n must be >= 0")
    return sorted(scores, key=lambda p: p[1], reverse=True)[:n]


def report(scores):
    best = top_n(scores, 3)
    lines = [f"{name}: {score}" for name, score in best]
    lines.append(f"first entry: {scores[0][0]}" if scores else "no entries")
    return "\\n".join(lines)
''',
    new='''def top_n(scores, n):
    """Return the n highest (name, score) pairs, highest first. `scores` is not modified."""
    if n < 0:
        raise ValueError("n must be >= 0")
    scores.sort(key=lambda p: p[1], reverse=True)
    return scores[:n]


def report(scores):
    best = top_n(scores, 3)
    lines = [f"{name}: {score}" for name, score in best]
    lines.append(f"first entry: {scores[0][0]}" if scores else "no entries")
    return "\\n".join(lines)
''', seed="scores.sort(key=lambda p: p[1], reverse=True)")

CASES["tags"] = dict(file="tags.py", kind="mutable default argument",
    intent="Let callers pass an existing tag list to extend.",
    old='''def normalize_tags(raw):
    """Split a comma-separated tag string into a new list of unique lowercase tags."""
    tags = []
    for t in raw.split(","):
        t = t.strip().lower()
        if t and t not in tags:
            tags.append(t)
    return tags
''',
    new='''def normalize_tags(raw, tags=[]):
    """Split a comma-separated tag string and append unique lowercase tags to `tags`.

    With no `tags` argument a new list is returned.
    """
    for t in raw.split(","):
        t = t.strip().lower()
        if t and t not in tags:
            tags.append(t)
    return tags
''', seed="def normalize_tags(raw, tags=[]):")

CASES["retry"] = dict(file="retry.py", kind="positional signature break",
    intent="Extract the delay calculation into a helper and add a `max_delay` cap.",
    old='''import time


def retry(fn, attempts=3, base=0.5, sleep=time.sleep):
    """Call fn until it succeeds; re-raise the last error after `attempts` tries."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            sleep(base * 2 ** i)
''',
    new='''import time


def backoff_delay(i, base, max_delay):
    """Delay before retry number i (0-based): base * 2**i, capped at max_delay."""
    return min(base * 2 ** i, max_delay)


def retry(fn, attempts=3, base=0.5, max_delay=30.0, sleep=time.sleep):
    """Call fn until it succeeds; re-raise the last error after `attempts` tries."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            sleep(backoff_delay(i, base, max_delay))
''', seed="def retry(fn, attempts=3, base=0.5, max_delay=30.0, sleep=time.sleep):")

CASES["money"] = dict(file="money.py", kind=None,
    intent="Reject negative amounts and amounts with more than two decimal places.",
    old='''from decimal import Decimal, InvalidOperation


def parse_amount(text):
    """Parse a decimal money amount like '12.50' into a Decimal."""
    try:
        return Decimal(text.strip())
    except InvalidOperation:
        raise ValueError(f"not an amount: {text!r}") from None
''',
    new='''from decimal import Decimal, InvalidOperation


def parse_amount(text):
    """Parse a nonnegative money amount with at most two decimal places into a Decimal."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError(f"not an amount: {text!r}") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a nonnegative amount: {text!r}")
    if value.as_tuple().exponent < -2:
        raise ValueError(f"more than two decimal places: {text!r}")
    return value
''', seed=None)
