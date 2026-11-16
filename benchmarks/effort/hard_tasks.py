"""Harder builder tasks: several files, bug-report style specs, hidden tests on sensible edge cases.

Each task has `files` (the starting workspace), `editable` (names the builder may change), optional
`create` (new names it must add), `ref` (reference contents of the changed or created files), `visible`
(test_solution.py) and `hidden` (test_hidden.py). Files not in `editable` must stay byte-identical.
"""

HARD_TASKS = {}

# ---------------------------------------------------------------- settings_leak
HARD_TASKS["settings_leak"] = dict(
    spec="Bug report: Service instances in one process interfere. Creating Service('b', {'timeout': 5}) "
         "changes the timeout of an existing Service('a'), and tags or headers added to one service show up "
         "on other services and on services created later. Fix it so each Service's settings share no "
         "mutable object with config.DEFAULTS, with other services, or with the overrides the caller "
         "passed, config.load returns fresh objects on every call, and config.DEFAULTS never changes. Keep everything else as it is, including that a "
         "nested dict override is merged into the default nested dict.",
    files={
        "config.py": '''DEFAULTS = {
    "retries": 3,
    "timeout": 10,
    "headers": {"User-Agent": "svc/1.0"},
    "tags": [],
}


def load(overrides=None):
    """Settings for one service: DEFAULTS updated with overrides (nested dicts are merged)."""
    settings = DEFAULTS
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key].update(value)
        else:
            settings[key] = value
    return settings
''',
        "service.py": '''import config


class Service:
    def __init__(self, name, overrides=None):
        self.name = name
        self.settings = config.load(overrides)

    def tag(self, label):
        self.settings["tags"].append(label)

    def header(self, key, value):
        self.settings["headers"][key] = value
''',
    },
    editable=("config.py", "service.py"),
    visible='''import unittest
from service import Service

class T(unittest.TestCase):
    def test_timeout_is_per_service(self):
        a = Service('a')
        Service('b', {'timeout': 5})
        self.assertEqual(a.settings['timeout'], 10)
    def test_tags_are_per_service(self):
        a, b = Service('a'), Service('b')
        a.tag('x')
        self.assertEqual(b.settings['tags'], [])
''',
    hidden='''import unittest
import config
from service import Service

class H(unittest.TestCase):
    def test_defaults_never_change(self):
        s = Service('a', {'retries': 9, 'headers': {'X': '1'}})
        s.header('Y', '2'); s.tag('t')
        self.assertEqual(config.DEFAULTS, {'retries': 3, 'timeout': 10,
                                           'headers': {'User-Agent': 'svc/1.0'}, 'tags': []})
    def test_nested_merge_kept(self):
        s = Service('a', {'headers': {'X': '1'}})
        self.assertEqual(s.settings['headers'], {'User-Agent': 'svc/1.0', 'X': '1'})
    def test_later_service_is_clean(self):
        Service('a').header('X', '1')
        self.assertEqual(Service('b').settings['headers'], {'User-Agent': 'svc/1.0'})
    def test_caller_list_not_aliased(self):
        mine = ['keep']
        s = Service('a', {'tags': mine})
        s.tag('new')
        self.assertEqual(mine, ['keep'])
        self.assertEqual(s.settings['tags'], ['keep', 'new'])
    def test_caller_dict_not_aliased(self):
        over = {'extra': {'k': 1}, 'headers': {'X': '1'}}
        s = Service('a', over)
        s.settings['extra']['k'] = 2; s.header('X', '9')
        self.assertEqual(over, {'extra': {'k': 1}, 'headers': {'X': '1'}})
    def test_load_returns_fresh_objects(self):
        a, b = config.load(), config.load()
        self.assertIsNot(a, b)
        self.assertIsNot(a['headers'], b['headers'])
        self.assertIsNot(a['tags'], config.DEFAULTS['tags'])
''',
    ref={"config.py": '''import copy

DEFAULTS = {
    "retries": 3,
    "timeout": 10,
    "headers": {"User-Agent": "svc/1.0"},
    "tags": [],
}


def load(overrides=None):
    """Settings for one service: DEFAULTS updated with overrides (nested dicts are merged)."""
    settings = copy.deepcopy(DEFAULTS)
    for key, value in (overrides or {}).items():
        value = copy.deepcopy(value)
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key].update(value)
        else:
            settings[key] = value
    return settings
'''})

# ---------------------------------------------------------------- cursor_pages
HARD_TASKS["cursor_pages"] = dict(
    spec="api.list_page uses offset cursors, so a client paging through a Store sees items repeated or "
         "skipped when items are added or removed between page requests. Change it so that, during a walk "
         "from cursor=None to the last page, every item that exists for the whole walk is returned exactly "
         "once, in ascending id order, whatever is added or removed between calls. Cursors stay opaque "
         "strings; next_cursor is None exactly when no item comes after the page; limit must be a positive "
         "int, otherwise ValueError; a malformed cursor raises ValueError. store.py is shared code: do not "
         "edit it.",
    files={
        "store.py": '''class Store:
    """Items keyed by unique int ids; ids() lists them in ascending order."""

    def __init__(self):
        self._items = {}

    def add(self, item_id, value):
        if item_id in self._items:
            raise KeyError(item_id)
        self._items[item_id] = value

    def remove(self, item_id):
        del self._items[item_id]

    def ids(self):
        return sorted(self._items)

    def get(self, item_id):
        return self._items[item_id]
''',
        "api.py": '''def list_page(store, cursor=None, limit=10):
    """Return (values, next_cursor); next_cursor is None on the last page."""
    ids = store.ids()
    start = int(cursor) if cursor else 0
    chunk = ids[start:start + limit]
    nxt = str(start + limit) if start + limit < len(ids) else None
    return [store.get(i) for i in chunk], nxt
''',
    },
    editable=("api.py",),
    visible='''import unittest
from api import list_page
from store import Store

def filled(ids):
    s = Store()
    for i in ids:
        s.add(i, f'v{i}')
    return s

class T(unittest.TestCase):
    def test_plain_walk(self):
        s = filled(range(1, 6))
        a, c = list_page(s, None, 2)
        b, c = list_page(s, c, 2)
        d, c = list_page(s, c, 2)
        self.assertEqual(a + b + d, ['v1', 'v2', 'v3', 'v4', 'v5'])
        self.assertIsNone(c)
    def test_insert_before_cursor_does_not_repeat(self):
        s = filled([10, 20, 30, 40])
        a, c = list_page(s, None, 2)
        s.add(5, 'v5')
        b, c = list_page(s, c, 2)
        self.assertEqual(a + b, ['v10', 'v20', 'v30', 'v40'])
''',
    hidden='''import unittest
from api import list_page
from store import Store

def filled(ids):
    s = Store()
    for i in ids:
        s.add(i, f'v{i}')
    return s

def walk(s, limit, between=lambda n: None):
    out, c, n = [], None, 0
    while True:
        vals, c = list_page(s, c, limit)
        out += vals
        if c is None:
            return out
        between(n); n += 1

class H(unittest.TestCase):
    def test_remove_before_cursor_does_not_skip(self):
        s = filled([1, 2, 3, 4, 5, 6])
        self.assertEqual(walk(s, 2, lambda n: s.remove(1) if n == 0 else None),
                         ['v1', 'v2', 'v3', 'v4', 'v5', 'v6'])
    def test_remove_last_returned_item(self):
        s = filled([1, 2, 3, 4, 5])
        a, c = list_page(s, None, 2)
        s.remove(2)
        b, c = list_page(s, c, 2)
        self.assertEqual(b, ['v3', 'v4'])
    def test_full_last_page_has_no_cursor(self):
        s = filled([1, 2, 3, 4])
        a, c = list_page(s, None, 2)
        b, c = list_page(s, c, 2)
        self.assertEqual(b, ['v3', 'v4'])
        self.assertIsNone(c)
    def test_negative_and_sparse_ids(self):
        s = filled([-7, -3, 0, 100, 2000])
        self.assertEqual(walk(s, 2, lambda n: s.add(-100 - n, 'x')),
                         ['v-7', 'v-3', 'v0', 'v100', 'v2000'])
    def test_empty_store(self):
        self.assertEqual(list_page(Store(), None, 3), ([], None))
    def test_cursor_is_str(self):
        _, c = list_page(filled([1, 2, 3]), None, 1)
        self.assertIsInstance(c, str)
    def test_bad_limit(self):
        for limit in (0, -1, 1.5, '2'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                list_page(filled([1]), None, limit)
    def test_malformed_cursor(self):
        with self.assertRaises(ValueError):
            list_page(filled([1, 2]), 'not a cursor!', 1)
''',
    ref={"api.py": '''def list_page(store, cursor=None, limit=10):
    """Return (values, next_cursor); next_cursor is None on the last page.

    The cursor is the last id returned, so a page starts after it whatever changed in between.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")
    after = None
    if cursor is not None:
        try:
            after = int(cursor)
        except (TypeError, ValueError):
            raise ValueError(f"malformed cursor {cursor!r}") from None
    ids = [i for i in store.ids() if after is None or i > after]
    chunk = ids[:limit]
    nxt = str(chunk[-1]) if len(ids) > limit else None
    return [store.get(i) for i in chunk], nxt
'''})

# ---------------------------------------------------------------- units_refactor
HARD_TASKS["units_refactor"] = dict(
    spec="Refactor: move the unit data out of units.py into a new module unit_table.py (create it), and "
         "make units.convert also handle the temperature units 'C', 'F' and 'K', which need an offset as "
         "well as a scale (100 C is 212 F and 373.15 K). Converting between different dimensions, or from "
         "or to an unknown unit, raises ValueError. legacy.py is an existing caller that must keep working "
         "unchanged; do not edit it.",
    files={
        "units.py": '''FACTORS = {"m": 1.0, "km": 1000.0, "cm": 0.01, "g": 0.001, "kg": 1.0, "lb": 0.45359237}
DIMENSION = {"m": "length", "km": "length", "cm": "length", "g": "mass", "kg": "mass", "lb": "mass"}


def convert(value, src, dst):
    if DIMENSION[src] != DIMENSION[dst]:
        raise ValueError(f"cannot convert {src} to {dst}")
    return value * FACTORS[src] / FACTORS[dst]
''',
        "legacy.py": '''"""Older callers. This module must keep working unchanged."""
import units
from units import convert


def shipping_weight_kg(pounds):
    return round(convert(pounds, dst="kg", src="lb"), 3)


def distance_km(metres):
    return units.convert(value=metres, src="m", dst="km")


def known_units():
    return sorted(units.FACTORS)
''',
    },
    editable=("units.py",),
    create=("unit_table.py",),
    visible='''import unittest
import legacy
from units import convert

class T(unittest.TestCase):
    def test_boiling(self):
        self.assertAlmostEqual(convert(100, 'C', 'F'), 212)
        self.assertAlmostEqual(convert(100, 'C', 'K'), 373.15)
    def test_legacy(self):
        self.assertEqual(legacy.shipping_weight_kg(10), 4.536)
    def test_table_module(self):
        import unit_table  # noqa: F401
''',
    hidden='''import os
import unittest
import legacy
from units import convert

class H(unittest.TestCase):
    def test_legacy_all(self):
        self.assertAlmostEqual(legacy.distance_km(2500), 2.5)
        self.assertTrue({'m', 'km', 'cm', 'g', 'kg', 'lb'} <= set(legacy.known_units()))
    def test_temperatures(self):
        self.assertAlmostEqual(convert(-40, 'C', 'F'), -40)
        self.assertAlmostEqual(convert(0, 'K', 'C'), -273.15)
        self.assertAlmostEqual(convert(32, 'F', 'K'), 273.15)
        self.assertAlmostEqual(convert(451, 'F', 'F'), 451)
        self.assertAlmostEqual(convert(300, 'K', 'F'), 80.33, places=2)
    def test_round_trip(self):
        for u in ('C', 'F', 'K'):
            for w in ('C', 'F', 'K'):
                with self.subTest(u=u, w=w):
                    self.assertAlmostEqual(convert(convert(37.5, u, w), w, u), 37.5)
    def test_linear_still_works(self):
        self.assertAlmostEqual(convert(3, 'km', 'cm'), 300000)
        self.assertAlmostEqual(convert(1, 'kg', 'lb'), 2.2046226, places=6)
    def test_errors(self):
        for src, dst in (('C', 'm'), ('kg', 'K'), ('m', 'g'), ('furlong', 'm'), ('m', 'parsec'), ('x', 'x')):
            with self.subTest(src=src, dst=dst), self.assertRaises(ValueError):
                convert(1, src, dst)
    def test_data_moved(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'unit_table.py'), encoding='utf-8') as f:
            table = f.read()
        with open(os.path.join(here, 'units.py'), encoding='utf-8') as f:
            units_src = f.read()
        self.assertIn('0.45359237', table)
        self.assertNotIn('0.45359237', units_src)
''',
    ref={
        "unit_table.py": '''"""Unit data: linear factors to the dimension's base unit, and temperature scales."""
FACTORS = {"m": 1.0, "km": 1000.0, "cm": 0.01, "g": 0.001, "kg": 1.0, "lb": 0.45359237}
DIMENSION = {"m": "length", "km": "length", "cm": "length", "g": "mass", "kg": "mass", "lb": "mass"}
# kelvin = (value + offset) * scale
TEMPERATURE = {"K": (0.0, 1.0), "C": (273.15, 1.0), "F": (459.67, 5 / 9)}
''',
        "units.py": '''from unit_table import DIMENSION, FACTORS, TEMPERATURE

__all__ = ["DIMENSION", "FACTORS", "TEMPERATURE", "convert"]


def _dimension(unit):
    if unit in TEMPERATURE:
        return "temperature"
    try:
        return DIMENSION[unit]
    except (KeyError, TypeError):
        raise ValueError(f"unknown unit {unit!r}") from None


def convert(value, src, dst):
    if _dimension(src) != _dimension(dst):
        raise ValueError(f"cannot convert {src} to {dst}")
    if src in TEMPERATURE:
        off_s, scale_s = TEMPERATURE[src]
        off_d, scale_d = TEMPERATURE[dst]
        return (value + off_s) * scale_s / scale_d - off_d
    return value * FACTORS[src] / FACTORS[dst]
''',
    })

# ---------------------------------------------------------------- install_order
HARD_TASKS["install_order"] = dict(
    spec="graph.deps_first recurses forever on a dependency cycle, and its order depends on dict insertion "
         "order. Fix graph.py and installer.py. deps_first(packages) returns every package after all of its "
         "dependencies; whenever several packages could come next, the alphabetically first comes next. It "
         "raises graph.CycleError (add it; a ValueError subclass) on a cycle, with the names of the packages "
         "on one cycle in its `cycle` attribute, and KeyError for a dependency that is not a key of "
         "`packages`. Extend installer.plan to plan(packages, installed=(), targets=None): it returns, in "
         "deps_first order, the targets and everything they depend on, directly or not (all packages when "
         "targets is None), minus the installed ones; an unknown target raises KeyError.",
    files={
        "graph.py": '''def deps_first(packages):
    """packages: dict of name -> list of dependency names. Return an install order, dependencies first."""
    order = []

    def visit(name):
        for dep in packages[name]:
            visit(dep)
        if name not in order:
            order.append(name)

    for name in packages:
        visit(name)
    return order
''',
        "installer.py": '''from graph import deps_first


def plan(packages, installed=()):
    """Names to install, in order, skipping those already installed."""
    return [n for n in deps_first(packages) if n not in installed]
''',
    },
    editable=("graph.py", "installer.py"),
    visible='''import unittest
from graph import CycleError, deps_first
from installer import plan

class T(unittest.TestCase):
    def test_alphabetical_ties(self):
        self.assertEqual(deps_first({'app': ['zlib', 'json'], 'json': [], 'zlib': []}),
                         ['json', 'zlib', 'app'])
    def test_cycle(self):
        with self.assertRaises(CycleError) as cm:
            deps_first({'a': ['b'], 'b': ['a']})
        self.assertIsInstance(cm.exception, ValueError)
    def test_plan_targets(self):
        pk = {'web': ['http'], 'http': ['tls'], 'tls': [], 'cli': []}
        self.assertEqual(plan(pk, installed={'tls'}, targets=['web']), ['http', 'web'])
''',
    hidden='''import unittest
from graph import CycleError, deps_first
from installer import plan

PK = {'d': ['b', 'c'], 'b': ['a'], 'c': ['a'], 'a': [], 'e': [], 'f': ['e', 'd']}

class H(unittest.TestCase):
    def test_exact_order(self):
        self.assertEqual(deps_first(PK), ['a', 'b', 'c', 'd', 'e', 'f'])
        self.assertEqual(deps_first({'z': [], 'y': ['z'], 'x': []}), ['x', 'z', 'y'])
        self.assertEqual(deps_first({'a': ['z'], 'b': [], 'z': []}), ['b', 'z', 'a'])
    def test_insertion_order_irrelevant(self):
        self.assertEqual(deps_first(dict(reversed(list(PK.items())))), deps_first(PK))
    def test_duplicate_dependency(self):
        self.assertEqual(deps_first({'a': ['b', 'b'], 'b': []}), ['b', 'a'])
    def test_empty(self):
        self.assertEqual(deps_first({}), [])
    def test_self_loop(self):
        with self.assertRaises(CycleError) as cm:
            deps_first({'a': ['a'], 'b': []})
        self.assertEqual(set(cm.exception.cycle), {'a'})
    def test_cycle_members(self):
        with self.assertRaises(CycleError) as cm:
            deps_first({'d': ['a'], 'a': ['b'], 'b': ['c'], 'c': ['a'], 'e': []})
        self.assertEqual(set(cm.exception.cycle), {'a', 'b', 'c'})
    def test_missing_dependency(self):
        with self.assertRaises(KeyError):
            deps_first({'a': ['ghost']})
    def test_plan_all_and_installed(self):
        self.assertEqual(plan(PK), ['a', 'b', 'c', 'd', 'e', 'f'])
        self.assertEqual(plan(PK, installed=('a', 'e')), ['b', 'c', 'd', 'f'])
    def test_plan_transitive_targets(self):
        self.assertEqual(plan(PK, targets=['d', 'e']), ['a', 'b', 'c', 'd', 'e'])
        self.assertEqual(plan(PK, installed=['b'], targets=['d']), ['a', 'c', 'd'])
    def test_plan_unknown_target(self):
        with self.assertRaises(KeyError):
            plan(PK, targets=['nope'])
    def test_plan_cycle_propagates(self):
        with self.assertRaises(CycleError):
            plan({'a': ['b'], 'b': ['a'], 'c': []}, targets=['a'])
''',
    ref={
        "graph.py": '''import heapq


class CycleError(ValueError):
    def __init__(self, cycle):
        super().__init__(f"dependency cycle: {' -> '.join(cycle)}")
        self.cycle = list(cycle)


def deps_first(packages):
    """packages: dict of name -> list of dependency names. Return an install order, dependencies first."""
    deps = {}
    for name, ds in packages.items():
        for d in ds:
            if d not in packages:
                raise KeyError(d)
        deps[name] = set(ds)
    state, stack = {}, []

    def visit(name):  # reports the first cycle found
        state[name] = 1
        stack.append(name)
        for d in sorted(deps[name]):
            if state.get(d) == 1:
                raise CycleError(stack[stack.index(d):])
            if d not in state:
                visit(d)
        stack.pop()
        state[name] = 2

    for name in sorted(deps):
        if name not in state:
            visit(name)
    users = {n: [] for n in deps}
    waiting = {n: len(ds) for n, ds in deps.items()}
    for n, ds in deps.items():
        for d in ds:
            users[d].append(n)
    ready = [n for n, k in waiting.items() if k == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        n = heapq.heappop(ready)
        order.append(n)
        for u in users[n]:
            waiting[u] -= 1
            if waiting[u] == 0:
                heapq.heappush(ready, u)
    return order
''',
        "installer.py": '''from graph import deps_first


def plan(packages, installed=(), targets=None):
    """Names to install, in deps_first order: targets and what they need, minus installed ones."""
    order = deps_first(packages)
    if targets is None:
        need = set(packages)
    else:
        need, todo = set(), list(targets)
        while todo:
            n = todo.pop()
            if n not in packages:
                raise KeyError(n)
            if n not in need:
                need.add(n)
                todo.extend(packages[n])
    installed = set(installed)
    return [n for n in order if n in need and n not in installed]
''',
    })

# ---------------------------------------------------------------- event_bus
HARD_TASKS["event_bus"] = dict(
    spec="Make bus.Bus safe to change from inside handlers. During an emit, handlers run in the order they "
         "were added; a handler that is removed during the emit before its turn (by itself or by another "
         "handler) is not called; a handler added during the emit is not called for that emit; removing a "
         "handler never causes another one to be skipped. A handler that raises does not stop the others: "
         "emit calls every remaining handler, then re-raises the first exception. off() for a handler that is "
         "not registered does nothing. plugins.once must keep working: its handler runs exactly once, even "
         "if the handler emits the same event again.",
    files={
        "bus.py": '''class Bus:
    def __init__(self):
        self._handlers = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        self._handlers[event].remove(handler)

    def emit(self, event, *args):
        for handler in self._handlers.get(event, []):
            handler(*args)
''',
        "plugins.py": '''def once(bus, event, handler):
    """Call handler for the next `event` only."""
    def wrapper(*args):
        bus.off(event, wrapper)
        handler(*args)
    bus.on(event, wrapper)
''',
    },
    editable=("bus.py", "plugins.py"),
    visible='''import unittest
from bus import Bus

class T(unittest.TestCase):
    def test_self_removal_does_not_skip_next(self):
        b, seen = Bus(), []
        def first():
            seen.append(1); b.off('e', first)
        b.on('e', first); b.on('e', lambda: seen.append(2))
        b.emit('e')
        self.assertEqual(seen, [1, 2])
    def test_raise_does_not_stop_others(self):
        b, seen = Bus(), []
        def bad():
            raise RuntimeError('x')
        b.on('e', bad); b.on('e', lambda: seen.append(2))
        with self.assertRaises(RuntimeError):
            b.emit('e')
        self.assertEqual(seen, [2])
''',
    hidden='''import unittest
from bus import Bus
from plugins import once

class H(unittest.TestCase):
    def test_removed_later_handler_not_called(self):
        b, seen = Bus(), []
        def second():
            seen.append(2)
        b.on('e', lambda: (seen.append(1), b.off('e', second)))
        b.on('e', second); b.on('e', lambda: seen.append(3))
        b.emit('e')
        self.assertEqual(seen, [1, 3])
    def test_added_during_emit_waits(self):
        b, seen, added = Bus(), [], []
        def late():
            seen.append('late')
        def adder():
            seen.append('x')
            if not added:
                added.append(1); b.on('e', late)
        b.on('e', adder)
        b.emit('e')
        self.assertEqual(seen, ['x'])
        b.emit('e')
        self.assertEqual(seen, ['x', 'x', 'late'])
    def test_first_exception_reraised_after_all(self):
        b, seen = Bus(), []
        def e1():
            raise KeyError('first')
        def e2():
            raise ValueError('second')
        b.on('e', e1); b.on('e', e2); b.on('e', lambda: seen.append('ran'))
        with self.assertRaises(KeyError):
            b.emit('e')
        self.assertEqual(seen, ['ran'])
    def test_off_unknown_is_noop(self):
        b = Bus()
        b.off('nothing', print)
        b.on('e', len)
        b.off('e', print)
    def test_args_and_order(self):
        b, seen = Bus(), []
        for i in range(4):
            b.on('e', lambda x, i=i: seen.append((i, x)))
        b.emit('e', 'v')
        self.assertEqual(seen, [(0, 'v'), (1, 'v'), (2, 'v'), (3, 'v')])
    def test_once_reentrant(self):
        b, seen = Bus(), []
        def h(n):
            seen.append(n)
            if n < 3:
                b.emit('e', n + 1)
        once(b, 'e', h)
        b.on('e', lambda n: seen.append(('tail', n)))
        b.emit('e', 1)
        self.assertEqual(seen.count(1), 1)
        self.assertEqual([x for x in seen if not isinstance(x, tuple)], [1])
        b.emit('e', 9)
        self.assertNotIn(9, seen)
    def test_once_after_mass_removal(self):
        b, seen = Bus(), []
        once(b, 'e', lambda: seen.append('a'))
        once(b, 'e', lambda: seen.append('b'))
        once(b, 'e', lambda: seen.append('c'))
        b.emit('e'); b.emit('e')
        self.assertEqual(seen, ['a', 'b', 'c'])
''',
    ref={"bus.py": '''class Bus:
    def __init__(self):
        self._handlers = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        handlers = self._handlers.get(event)
        if handlers and handler in handlers:
            # Rebind instead of mutating, so a running emit keeps a stable snapshot.
            i = handlers.index(handler)
            self._handlers[event] = handlers[:i] + handlers[i + 1:]

    def emit(self, event, *args):
        first = None
        for handler in list(self._handlers.get(event, [])):
            if handler not in self._handlers.get(event, []):
                continue  # removed earlier in this emit
            try:
                handler(*args)
            except Exception as e:
                if first is None:
                    first = e
        if first is not None:
            raise first
'''})

# ---------------------------------------------------------------- money_cents
HARD_TASKS["money_cents"] = dict(
    spec="Prices are floats, so totals drift (three items at 0.10 do not add up to 0.30) and round() rounds "
         "half to even. Change cart.py, tax.py and receipt.py to work in integer cents. Cart.add(sku, "
         "unit_price, qty=1) takes unit_price as a str with digits, a point and exactly two decimals, such as "
         "'19.99' (anything else, including a negative price or a non-str, raises ValueError) and qty as a "
         "positive int (otherwise ValueError). subtotal() and total(region) return int cents. "
         "tax.tax_for(amount_cents, region) returns int cents, rounded half up at the exact half cent; an "
         "unknown region raises KeyError. receipt.render keeps its output format, with amounts as d.dd.",
    files={
        "cart.py": '''from tax import tax_for


class Cart:
    def __init__(self):
        self.lines = []  # (sku, unit_price, qty)

    def add(self, sku, unit_price, qty=1):
        self.lines.append((sku, unit_price, qty))

    def subtotal(self):
        return sum(price * qty for _, price, qty in self.lines)

    def total(self, region):
        return self.subtotal() + tax_for(self.subtotal(), region)
''',
        "tax.py": '''RATES = {"US-CA": 0.0725, "US-OR": 0.0, "DE": 0.19}


def tax_for(amount, region):
    return round(amount * RATES[region], 2)
''',
        "receipt.py": '''def render(cart, region):
    lines = [f"{sku} x{qty} {price * qty:.2f}" for sku, price, qty in cart.lines]
    lines.append(f"TOTAL {cart.total(region):.2f}")
    return "\\n".join(lines)
''',
    },
    editable=("cart.py", "tax.py", "receipt.py"),
    visible='''import unittest
from cart import Cart
from receipt import render

class T(unittest.TestCase):
    def test_no_drift(self):
        c = Cart()
        c.add('a', '0.10'); c.add('b', '0.10'); c.add('c', '0.10')
        self.assertEqual(c.subtotal(), 30)
    def test_render(self):
        c = Cart()
        c.add('pen', '1.25', 2); c.add('pad', '3.00')
        self.assertEqual(render(c, 'US-OR'), 'pen x2 2.50\\npad x1 3.00\\nTOTAL 5.50')
''',
    hidden='''import unittest
from cart import Cart
from receipt import render
from tax import tax_for

class H(unittest.TestCase):
    def test_half_up_at_exact_half(self):
        self.assertEqual(tax_for(1000, 'US-CA'), 73)   # 72.5 cents
        self.assertEqual(tax_for(250, 'DE'), 48)       # 47.5 cents
        self.assertEqual(tax_for(1, 'DE'), 0)
        self.assertEqual(tax_for(0, 'US-CA'), 0)
        self.assertIsInstance(tax_for(999, 'US-CA'), int)
    def test_unknown_region(self):
        with self.assertRaises(KeyError):
            tax_for(100, 'XX')
    def test_total_with_tax(self):
        c = Cart()
        c.add('a', '10.00')
        self.assertEqual(c.total('US-CA'), 1073)
        self.assertEqual(render(c, 'DE'), 'a x1 10.00\\nTOTAL 11.90')
    def test_big_and_small(self):
        c = Cart()
        c.add('x', '1234.56', 3); c.add('y', '0.01'); c.add('z', '0.00', 5)
        self.assertEqual(c.subtotal(), 370369)
        self.assertEqual(render(c, 'US-OR'), 'x x3 3703.68\\ny x1 0.01\\nz x5 0.00\\nTOTAL 3703.69')
    def test_bad_prices(self):
        for p in ('1.5', '1.999', '-1.00', 'abc', '1,00', '', '1.', '1e2', 1.25, 125, None, '+1.00', '1.0 '):
            with self.subTest(p=p), self.assertRaises(ValueError):
                Cart().add('s', p)
    def test_bad_qty(self):
        for q in (0, -2, 1.5, '2'):
            with self.subTest(q=q), self.assertRaises(ValueError):
                Cart().add('s', '1.00', q)
    def test_int_results(self):
        c = Cart()
        c.add('a', '0.99', 3)
        self.assertIsInstance(c.subtotal(), int)
        self.assertIsInstance(c.total('US-CA'), int)
''',
    ref={
        "cart.py": '''import re

from tax import tax_for

_PRICE = re.compile(r"[0-9]+\\.[0-9]{2}", re.A)


def to_cents(text):
    if not isinstance(text, str) or not _PRICE.fullmatch(text):
        raise ValueError(f"price must look like '19.99', got {text!r}")
    whole, frac = text.split(".")
    return int(whole) * 100 + int(frac)


class Cart:
    def __init__(self):
        self.lines = []  # (sku, unit_price_cents, qty)

    def add(self, sku, unit_price, qty=1):
        cents = to_cents(unit_price)
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise ValueError(f"qty must be a positive int, got {qty!r}")
        self.lines.append((sku, cents, qty))

    def subtotal(self):
        return sum(price * qty for _, price, qty in self.lines)

    def total(self, region):
        sub = self.subtotal()
        return sub + tax_for(sub, region)
''',
        "tax.py": '''from decimal import ROUND_HALF_UP, Decimal

RATES = {"US-CA": Decimal("0.0725"), "US-OR": Decimal("0"), "DE": Decimal("0.19")}


def tax_for(amount, region):
    """Tax in int cents on amount int cents, rounded half up."""
    return int((Decimal(amount) * RATES[region]).quantize(Decimal(1), rounding=ROUND_HALF_UP))
''',
        "receipt.py": '''def money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def render(cart, region):
    lines = [f"{sku} x{qty} {money(price * qty)}" for sku, price, qty in cart.lines]
    lines.append(f"TOTAL {money(cart.total(region))}")
    return "\\n".join(lines)
''',
    })
