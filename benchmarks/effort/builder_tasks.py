"""Builder tasks for the effort benchmark: spec, stub, visible tests, hidden tests, reference solution."""

TASKS = {}

# ---------------------------------------------------------------- ranges
TASKS["ranges"] = dict(
    spec="Implement `parse_ranges(text)`. Accept blank (empty or whitespace-only) input as `[]`. Otherwise "
         "accept comma-separated segments; each segment is a nonnegative decimal integer `N` or an inclusive "
         "interval `A-B`, with optional whitespace around the whole segment (not inside it). Reject empty or "
         "malformed segments, negative numbers, and reversed intervals (A > B) with `ValueError`. Sort and "
         "merge overlapping or adjacent intervals. Return a list of `(start, end)` tuples.",
    stub='def parse_ranges(text):\n    raise NotImplementedError\n',
    visible='''import unittest
from solution import parse_ranges

class T(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_ranges('1-3, 7, 10-12'), [(1, 3), (7, 7), (10, 12)])
    def test_merge(self):
        self.assertEqual(parse_ranges('5-7,1-3,3-5,9,8'), [(1, 9)])
    def test_blank(self):
        self.assertEqual(parse_ranges('  '), [])
    def test_invalid(self):
        for v in ('1,', ',1', '1,,2', '-1', '3-1', '1.5', 'a', '1-2-3'):
            with self.subTest(v=v), self.assertRaises(ValueError):
                parse_ranges(v)
''',
    hidden='''import unittest
from solution import parse_ranges

class H(unittest.TestCase):
    def test_spaces(self):
        self.assertEqual(parse_ranges(' 4-6 , 1 '), [(1, 1), (4, 6)])
    def test_adjacent_singles(self):
        self.assertEqual(parse_ranges('1,2,3'), [(1, 3)])
    def test_zero_equal(self):
        self.assertEqual(parse_ranges('0,5-5'), [(0, 0), (5, 5)])
    def test_contained(self):
        self.assertEqual(parse_ranges('1-10,3-4'), [(1, 10)])
    def test_empty(self):
        self.assertEqual(parse_ranges(''), [])
    def test_more_invalid(self):
        for v in ('1-', '-', '1 2', '1--2', ' , ', '2-1,5', '4 - 6'):
            with self.subTest(v=v), self.assertRaises(ValueError):
                parse_ranges(v)
''',
    ref='''import re
def parse_ranges(text):
    if not text.strip():
        return []
    out = []
    for seg in text.split(','):
        m = re.fullmatch(r'\\s*(\\d+)(?:-(\\d+))?\\s*', seg, re.A)
        if not m:
            raise ValueError(seg)
        a = int(m[1]); b = int(m[2]) if m[2] is not None else a
        if b < a:
            raise ValueError(seg)
        out.append((a, b))
    out.sort(); merged = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged
''')

# ---------------------------------------------------------------- summary
TASKS["summary"] = dict(
    spec="Implement `summarize_results(rows)`. `rows` is a list of dicts; each row has exactly the keys `id` "
         "(a nonempty str, unique across rows), `status` (one of 'pass', 'fail', 'skip') and `duration_ms` "
         "(a nonnegative int; bool is not an int here). Return `{'pass': n, 'fail': n, 'skip': n, "
         "'total_ms': n, 'slowest_id': id}` where slowest_id is the id of the row with the largest duration_ms "
         "(ties: the earliest row). Empty input gives zero counts and `slowest_id: None`. Raise `ValueError` "
         "for any missing, extra or invalid key or value, or a duplicate id. Raise `ValueError` if a row is not a dict. Do not mutate the input.",
    stub='def summarize_results(rows):\n    raise NotImplementedError\n',
    visible='''import unittest
from solution import summarize_results

class T(unittest.TestCase):
    def test_basic(self):
        rows = [{'id': 'a', 'status': 'pass', 'duration_ms': 5},
                {'id': 'b', 'status': 'fail', 'duration_ms': 9},
                {'id': 'c', 'status': 'skip', 'duration_ms': 0}]
        self.assertEqual(summarize_results(rows),
                         {'pass': 1, 'fail': 1, 'skip': 1, 'total_ms': 14, 'slowest_id': 'b'})
    def test_empty(self):
        self.assertEqual(summarize_results([]),
                         {'pass': 0, 'fail': 0, 'skip': 0, 'total_ms': 0, 'slowest_id': None})
    def test_bad_status(self):
        with self.assertRaises(ValueError):
            summarize_results([{'id': 'a', 'status': 'ok', 'duration_ms': 1}])
''',
    hidden='''import copy, unittest
from solution import summarize_results

def r(i, s='pass', d=1):
    return {'id': i, 'status': s, 'duration_ms': d}

class H(unittest.TestCase):
    def test_tie_first(self):
        self.assertEqual(summarize_results([r('x', d=3), r('y', d=3)])['slowest_id'], 'x')
    def test_zero_durations(self):
        self.assertEqual(summarize_results([r('x', d=0), r('y', d=0)])['slowest_id'], 'x')
    def test_no_mutation(self):
        rows = [r('a', d=2), r('b', 'fail', 7)]
        before = copy.deepcopy(rows)
        summarize_results(rows)
        self.assertEqual(rows, before)
    def test_invalid(self):
        bad = [[r('a'), r('a')], [{'id': 'a', 'status': 'pass'}], [r('a', d=-1)], [r('a', d=True)],
               [r('a', d=1.0)], [r('', d=1)], [r(3, d=1)], [dict(r('a'), extra=1)], ['row'],
               [{'status': 'pass', 'duration_ms': 1}]]
        for rows in bad:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                summarize_results(rows)
''',
    ref='''def summarize_results(rows):
    out = {'pass': 0, 'fail': 0, 'skip': 0, 'total_ms': 0, 'slowest_id': None}
    seen, best = set(), -1
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'status', 'duration_ms'}:
            raise ValueError(row)
        i, s, d = row['id'], row['status'], row['duration_ms']
        if not isinstance(i, str) or not i or i in seen:
            raise ValueError(i)
        if s not in ('pass', 'fail', 'skip'):
            raise ValueError(s)
        if type(d) is not int or d < 0:
            raise ValueError(d)
        seen.add(i); out[s] += 1; out['total_ms'] += d
        if d > best:
            best, out['slowest_id'] = d, i
    return out
''')

# ---------------------------------------------------------------- semver
TASKS["semver"] = dict(
    spec="Implement `satisfies(version, spec)` returning bool. A version is `MAJOR.MINOR.PATCH` (decimal, no "
         "leading zeros except `0` itself) optionally followed by `-PRERELEASE`, where PRERELEASE is one or "
         "more dot-separated identifiers of `[0-9A-Za-z-]`, nonempty, numeric identifiers without leading "
         "zeros. Ordering follows SemVer 2.0.0: compare MAJOR, MINOR, PATCH numerically; a version with a "
         "prerelease is lower than the same version without one; prerelease identifiers compare left to right, "
         "numeric ones numerically, alphanumeric ones in ASCII order, numeric lower than alphanumeric, and a "
         "shorter identifier list is lower when all preceding identifiers are equal. `spec` is one or more "
         "comparators separated by whitespace; leading and trailing whitespace is ignored. All comparators must hold. A comparator is `OPversion` with OP in "
         "`>=`, `<=`, `>`, `<`, `=`, or no OP (meaning `=`); or `^X.Y.Z` (>=X.Y.Z and below the next major if "
         "X>0, else below the next minor if Y>0, else below 0.0.Z+1); or `~X.Y.Z` (>=X.Y.Z and below X.Y+1.0). "
         "The upper bounds of ^ and ~ are release versions, so e.g. `2.0.0-rc.1` is below 2.0.0 and DOES "
         "satisfy `^1.0.0`. Raise `ValueError` for any invalid version or comparator, or an empty spec.",
    stub='def satisfies(version, spec):\n    raise NotImplementedError\n',
    visible='''import unittest
from solution import satisfies

class T(unittest.TestCase):
    def test_ops(self):
        self.assertTrue(satisfies('1.2.3', '>=1.2.0 <2.0.0'))
        self.assertFalse(satisfies('2.0.0', '>=1.2.0 <2.0.0'))
        self.assertTrue(satisfies('1.2.3', '1.2.3'))
    def test_caret_tilde(self):
        self.assertTrue(satisfies('1.9.9', '^1.2.3'))
        self.assertFalse(satisfies('1.3.0', '~1.2.3'))
    def test_prerelease_order(self):
        self.assertTrue(satisfies('1.0.0-alpha', '<1.0.0'))
    def test_invalid(self):
        for v, s in (('1.2', '>=1.0.0'), ('1.2.3', '>>1.0.0'), ('01.2.3', '>=1.0.0'), ('1.2.3', '')):
            with self.subTest(v=v, s=s), self.assertRaises(ValueError):
                satisfies(v, s)
''',
    hidden='''import unittest
from solution import satisfies

class H(unittest.TestCase):
    def test_semver_precedence_chain(self):
        chain = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta', '1.0.0-beta.2',
                 '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0']
        for lo, hi in zip(chain, chain[1:]):
            with self.subTest(lo=lo, hi=hi):
                self.assertTrue(satisfies(lo, '<' + hi))
                self.assertFalse(satisfies(hi, '<' + lo))
                self.assertTrue(satisfies(hi, '>' + lo))
    def test_numeric_not_lexical(self):
        self.assertTrue(satisfies('1.10.0', '>1.9.0'))
        self.assertTrue(satisfies('1.0.0-2', '<1.0.0-10'))
    def test_caret_zero(self):
        self.assertTrue(satisfies('0.2.9', '^0.2.3'))
        self.assertFalse(satisfies('0.3.0', '^0.2.3'))
        self.assertTrue(satisfies('0.0.3', '^0.0.3'))
        self.assertFalse(satisfies('0.0.4', '^0.0.3'))
        self.assertFalse(satisfies('0.2.2', '^0.2.3'))
    def test_upper_bounds_release(self):
        self.assertTrue(satisfies('2.0.0-rc.1', '^1.0.0'))
        self.assertTrue(satisfies('1.3.0-a', '~1.2.0'))
        self.assertFalse(satisfies('1.3.0', '~1.2.0'))
    def test_eq_prerelease(self):
        self.assertFalse(satisfies('1.0.0-a', '=1.0.0'))
        self.assertTrue(satisfies('1.0.0-a', '1.0.0-a'))
        self.assertTrue(satisfies('1.0.0', '<=1.0.0 >=1.0.0'))
    def test_whitespace(self):
        self.assertTrue(satisfies('1.5.0', '  >=1.0.0    <2.0.0 '))
    def test_more_invalid(self):
        for v, s in (('1.2.3-', '>=1.0.0'), ('1.2.3-a..b', '>=1.0.0'), ('1.2.3-01', '>=1.0.0'),
                     ('1.2.3', '>= 1.0.0'), ('1.2.3', '^1.2'), ('1.2.3', '   '), ('1.2.3-a_b', '>=1.0.0'),
                     ('v1.2.3', '>=1.0.0'), ('1.2.3', '=>1.0.0'), ('1.2.3', '^1.0.0-'), ('1.2.3', '~01.0.0')):
            with self.subTest(v=v, s=s), self.assertRaises(ValueError):
                satisfies(v, s)
    def test_alnum_with_leading_zero_ok(self):
        self.assertTrue(satisfies('1.0.0-0a', '<1.0.0'))
''',
    ref='''import re
_ID = r'(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)'
_V = re.compile(r'(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-(' + _ID + r'(?:\\.' + _ID + r')*))?', re.A)

def _parse(v):
    m = _V.fullmatch(v)
    if not m:
        raise ValueError(v)
    pre = tuple((0, int(x), '') if x.isdigit() else (1, 0, x) for x in m[4].split('.')) if m[4] else None
    return (int(m[1]), int(m[2]), int(m[3])), pre

def _key(p):
    core, pre = p
    return (core, (1,) if pre is None else (0, pre))

def satisfies(version, spec):
    v = _key(_parse(version))
    parts = spec.split()
    if not parts:
        raise ValueError('empty spec')
    for c in parts:
        m = re.fullmatch(r'(>=|<=|>|<|=|\\^|~)?(.*)', c, re.S)
        op, rest = m[1] or '=', m[2]
        p = _parse(rest)
        t = _key(p)
        if op in '^~':
            (X, Y, Z), _ = p
            if op == '~':
                hi = (X, Y + 1, 0)
            elif X > 0:
                hi = (X + 1, 0, 0)
            elif Y > 0:
                hi = (0, Y + 1, 0)
            else:
                hi = (0, 0, Z + 1)
            ok = t <= v < (hi, (1,))
        else:
            ok = {'>=': v >= t, '<=': v <= t, '>': v > t, '<': v < t, '=': v == t}[op]
        if not ok:
            return False
    return True
''')

# ---------------------------------------------------------------- csvsplit
TASKS["csvsplit"] = dict(
    spec="Implement `split_csv_line(line, delimiter=',')` returning a list of str fields for one CSV record "
         "(no newlines in `line`). Fields are separated by `delimiter` (a single character, not `\"`; else "
         "`ValueError`). A field is either unquoted (taken verbatim, including any spaces; it must not "
         "contain `\"`) or quoted: it starts with `\"` immediately after the start or a delimiter, ends with a "
         "`\"` immediately followed by a delimiter or end of line, may contain delimiters, and `\"\"` inside it "
         "means one `\"`. The empty line is one empty field `['']`. Raise `ValueError` for an unterminated "
         "quoted field, a `\"` inside an unquoted field, or any character between a closing quote and the "
         "next delimiter.",
    stub="def split_csv_line(line, delimiter=','):\n    raise NotImplementedError\n",
    visible='''import unittest
from solution import split_csv_line

class T(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(split_csv_line('a,b,c'), ['a', 'b', 'c'])
    def test_quoted(self):
        self.assertEqual(split_csv_line('"a,b",c'), ['a,b', 'c'])
    def test_escaped_quote(self):
        self.assertEqual(split_csv_line('"say ""hi""",x'), ['say "hi"', 'x'])
    def test_unterminated(self):
        with self.assertRaises(ValueError):
            split_csv_line('"abc,d')
''',
    hidden='''import unittest
from solution import split_csv_line

class H(unittest.TestCase):
    def test_empty_line(self):
        self.assertEqual(split_csv_line(''), [''])
    def test_empty_fields(self):
        self.assertEqual(split_csv_line(',,'), ['', '', ''])
        self.assertEqual(split_csv_line('a,'), ['a', ''])
    def test_empty_quoted(self):
        self.assertEqual(split_csv_line('"",x,""'), ['', 'x', ''])
    def test_spaces_verbatim(self):
        self.assertEqual(split_csv_line(' a , b '), [' a ', ' b '])
    def test_only_escaped_quote(self):
        self.assertEqual(split_csv_line('""""'), ['"'])
    def test_delimiter(self):
        self.assertEqual(split_csv_line('a;"b;c";d', ';'), ['a', 'b;c', 'd'])
        self.assertEqual(split_csv_line('a,b;c', ';'), ['a,b', 'c'])
        self.assertEqual(split_csv_line('a\\tb', '\\t'), ['a', 'b'])
    def test_bad_delimiter(self):
        for d in ('', ';;', '"'):
            with self.subTest(d=d), self.assertRaises(ValueError):
                split_csv_line('a', d)
    def test_errors(self):
        for line in ('"a"b,c', 'a"b,c', '"a" ,b', '"', '"a""', 'x,"y', ' "a",b'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                split_csv_line(line)
''',
    ref='''def split_csv_line(line, delimiter=','):
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter == '"':
        raise ValueError(delimiter)
    fields, i, n = [], 0, len(line)
    while True:
        if i < n and line[i] == '"':
            i += 1; buf = []
            while True:
                if i >= n:
                    raise ValueError('unterminated')
                ch = line[i]
                if ch == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        buf.append('"'); i += 2; continue
                    i += 1; break
                buf.append(ch); i += 1
            fields.append(''.join(buf))
            if i == n:
                return fields
            if line[i] != delimiter:
                raise ValueError('text after quote')
            i += 1
        else:
            j = line.find(delimiter, i)
            end = n if j < 0 else j
            field = line[i:end]
            if '"' in field:
                raise ValueError('quote in unquoted field')
            fields.append(field)
            if j < 0:
                return fields
            i = j + 1
''')

# ---------------------------------------------------------------- ttlcache
TASKS["ttlcache"] = dict(
    spec="Implement class `TTLCache(capacity, ttl, clock)`. `capacity` is an int >= 1, `ttl` a number > 0, "
         "else `ValueError`. `clock` is a zero-argument callable returning the current time in seconds "
         "(never call `time` yourself). An entry written at time t is expired once `clock() - t >= ttl`. "
         "`put(key, value)` stores or overwrites the entry, resets its write time and makes it most recently "
         "used; if this would exceed capacity, first discard all expired entries, then if still over capacity "
         "evict the least recently used entry. `get(key, default=None)` returns the value of a live entry and "
         "makes it most recently used (without changing its write time); an expired or missing key returns "
         "`default` (and an expired one is removed). `len(cache)` is the number of live (unexpired) entries. "
         "`key in cache` is True only for live entries and does not change recency.",
    stub='class TTLCache:\n    def __init__(self, capacity, ttl, clock):\n        raise NotImplementedError\n',
    visible='''import unittest
from solution import TTLCache

class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t

class T(unittest.TestCase):
    def test_basic(self):
        c = TTLCache(2, 10, Clock())
        c.put('a', 1)
        self.assertEqual(c.get('a'), 1)
        self.assertIsNone(c.get('zz'))
    def test_expiry(self):
        k = Clock(); c = TTLCache(2, 10, k)
        c.put('a', 1); k.t = 10
        self.assertEqual(c.get('a', 'gone'), 'gone')
    def test_lru(self):
        c = TTLCache(2, 100, Clock())
        c.put('a', 1); c.put('b', 2); c.get('a'); c.put('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 1)
''',
    hidden='''import unittest
from solution import TTLCache

class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t

class H(unittest.TestCase):
    def test_invalid(self):
        for cap, ttl in ((0, 1), (-1, 1), (1, 0), (1, -5)):
            with self.subTest(cap=cap, ttl=ttl), self.assertRaises(ValueError):
                TTLCache(cap, ttl, Clock())
    def test_just_before_expiry(self):
        k = Clock(); c = TTLCache(1, 10, k)
        c.put('a', 1); k.t = 9.999
        self.assertEqual(c.get('a'), 1)
    def test_get_does_not_refresh_ttl(self):
        k = Clock(); c = TTLCache(2, 10, k)
        c.put('a', 1); k.t = 6; c.get('a'); k.t = 10
        self.assertIsNone(c.get('a'))
    def test_put_overwrite_refreshes(self):
        k = Clock(); c = TTLCache(2, 10, k)
        c.put('a', 1); k.t = 6; c.put('a', 2); k.t = 12
        self.assertEqual(c.get('a'), 2)
        self.assertEqual(len(c), 1)
    def test_overwrite_makes_mru_and_no_eviction(self):
        c = TTLCache(2, 100, Clock())
        c.put('a', 1); c.put('b', 2); c.put('a', 3); c.put('c', 4)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 3)
        self.assertEqual(c.get('c'), 4)
    def test_expired_evicted_before_lru(self):
        k = Clock(); c = TTLCache(2, 10, k)
        c.put('old', 1); k.t = 5; c.put('fresh', 2); c.get('old')
        k.t = 11  # old expired, fresh live; old is MRU
        c.put('new', 3)
        self.assertEqual(c.get('fresh'), 2)
        self.assertEqual(c.get('new'), 3)
    def test_len_and_contains(self):
        k = Clock(); c = TTLCache(3, 10, k)
        c.put('a', 1); k.t = 5; c.put('b', 2)
        self.assertEqual(len(c), 2)
        k.t = 10
        self.assertEqual(len(c), 1)
        self.assertNotIn('a', c)
        self.assertIn('b', c)
    def test_contains_no_recency(self):
        c = TTLCache(2, 100, Clock())
        c.put('a', 1); c.put('b', 2)
        self.assertIn('a', c)
        c.put('c', 3)
        self.assertNotIn('a', c)
    def test_falsy_values(self):
        c = TTLCache(2, 100, Clock())
        c.put('a', None); c.put('b', 0)
        self.assertIsNone(c.get('a', 'x'))
        self.assertEqual(c.get('b', 'x'), 0)
        self.assertIn('a', c)
''',
    ref='''from collections import OrderedDict

class TTLCache:
    def __init__(self, capacity, ttl, clock):
        if not isinstance(capacity, int) or capacity < 1 or not ttl > 0:
            raise ValueError
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.d = OrderedDict()

    def _live(self, key, now):
        e = self.d.get(key)
        return e is not None and now - e[1] < self.ttl

    def put(self, key, value):
        now = self.clock()
        self.d.pop(key, None)
        if len(self.d) >= self.capacity:
            for k in [k for k in self.d if not self._live(k, now)]:
                del self.d[k]
            while len(self.d) >= self.capacity:
                self.d.popitem(last=False)
        self.d[key] = (value, now)

    def get(self, key, default=None):
        now = self.clock()
        if key not in self.d:
            return default
        if not self._live(key, now):
            del self.d[key]
            return default
        self.d.move_to_end(key)
        return self.d[key][0]

    def __len__(self):
        now = self.clock()
        return sum(1 for k in self.d if self._live(k, now))

    def __contains__(self, key):
        return self._live(key, self.clock())
''')

# ---------------------------------------------------------------- ini
TASKS["ini"] = dict(
    spec="Implement `parse_ini(text)` returning `{section: {key: value}}`. Process `text.splitlines()`. Each "
         "line, after stripping surrounding whitespace, is: empty (ignored); a comment starting with `;` or "
         "`#` (ignored); a section header `[name]` where name is stripped and must be nonempty and must not "
         "contain `[` or `]`; or `key = value`, split at the FIRST `=`, key and value stripped, key nonempty "
         "and stored lowercased, value kept as is (may be empty, may contain `=`, `;` and `#`, no inline "
         "comment removal). Keys before any section header go in section `''`, which appears in the result "
         "only if it has at least one key. Section names are case-sensitive; every header creates its section "
         "even if it stays empty. Raise `ValueError` for a repeated section header, a repeated key within a "
         "section (after lowercasing), or any other line.",
    stub='def parse_ini(text):\n    raise NotImplementedError\n',
    visible='''import unittest
from solution import parse_ini

class T(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_ini('[a]\\nx = 1\\ny=2\\n'), {'a': {'x': '1', 'y': '2'}})
    def test_comments(self):
        self.assertEqual(parse_ini('; c\\n# c\\n[s]\\nk = v'), {'s': {'k': 'v'}})
    def test_bad_line(self):
        with self.assertRaises(ValueError):
            parse_ini('[s]\\njunk')
''',
    hidden='''import unittest
from solution import parse_ini

class H(unittest.TestCase):
    def test_global_section(self):
        self.assertEqual(parse_ini('k=1\\n[s]\\nj=2'), {'': {'k': '1'}, 's': {'j': '2'}})
        self.assertNotIn('', parse_ini('[s]\\nj=2'))
    def test_empty_section_kept(self):
        self.assertEqual(parse_ini('[a]\\n[b]\\nk=v'), {'a': {}, 'b': {'k': 'v'}})
    def test_value_rules(self):
        self.assertEqual(parse_ini('[s]\\nurl = a=b;c#d\\nempty =\\n  Key  =  sp  ace  '),
                         {'s': {'url': 'a=b;c#d', 'empty': '', 'key': 'sp  ace'}})
    def test_case(self):
        self.assertEqual(parse_ini('[S]\\nA=1\\n[s]\\na=2'), {'S': {'a': '1'}, 's': {'a': '2'}})
    def test_header_strip(self):
        self.assertEqual(parse_ini('  [  my sec ]  \\nk=v'), {'my sec': {'k': 'v'}})
    def test_indented_comment(self):
        self.assertEqual(parse_ini('[s]\\n   ; note\\n\\t# x\\nk=v'), {'s': {'k': 'v'}})
    def test_errors(self):
        for t in ('[a]\\n[a]', '[a]\\nk=1\\nK=2', '[]', '[ ]', '[a', 'a]', '[a]x', '= v', '[s]\\n  =v',
                  '[a[b]]', 'k=1\\nk=2'):
            with self.subTest(t=t), self.assertRaises(ValueError):
                parse_ini(t)
    def test_crlf(self):
        self.assertEqual(parse_ini('[s]\\r\\nk=v\\r\\n'), {'s': {'k': 'v'}})
''',
    ref='''def parse_ini(text):
    out, cur = {}, ''
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ';#':
            continue
        if line.startswith('[') and line.endswith(']'):
            name = line[1:-1].strip()
            if not name or '[' in name or ']' in name or name in out:
                raise ValueError(raw)
            out[name] = {}; cur = name
            continue
        if '=' not in line:
            raise ValueError(raw)
        k, v = line.split('=', 1)
        k = k.strip().lower()
        if not k:
            raise ValueError(raw)
        sec = out.setdefault(cur, {})
        if k in sec:
            raise ValueError(raw)
        sec[k] = v.strip()
    return out
''')
