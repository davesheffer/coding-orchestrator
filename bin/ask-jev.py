#!/usr/bin/env python3
"""Ask Jev about files without reading them into the agent's context.

This script reads the files, sends their text to Jev as `state`, and prints only
Jev's typed answers, so a yes/no, multiple-choice or score judgement about a file
costs a fraction of a cent instead of the file's tokens in the agent's window.
Use Read when you need the code itself and grep for exact lookups; Jev is for
judgements ("does this file validate tokens?"), not counting or math.

Usage:
  ask-jev.py -q QUESTIONS [PATH_OR_GLOB ...] [--each] [--recursive] [--state TEXT] [--stdin]

QUESTIONS is a JSON object, or @file holding one, keyed by your own ids. Write
each question against the state Jev sees (`files`, `content`, `text`, `input`):
  {"auth": {"type": "noul", "instructions": "Does a file in `files` validate auth tokens?"},
   "kind": {"type": "choice", "instructions": "What is the main role of `content`?",
            "criteria": {"handler": "HTTP request handler", "util": "shared helper"}},
   "risk": {"type": "score", "instructions": "How risky is changing `content`?",
            "criteria": ["safe to change", "needs care", "dangerous"]}}
noul answers carry a 0-1 `noul` probability; choice answers carry `choice` and
`confidence` (an `other` option is added unless one exists); score answers carry
a float `score` that can fall between levels. Ask every question you need in one
call: they share the state.

Default: one call whose state is {"files": {path: text}, "text": --state,
"input": stdin}, for up to 20 files. --each: one call per file with state
{"path", "content"}, for up to 255 files, several at a time.

It runs only inside a git repository, and files must be inside it: a glob or
directory whose base is outside is skipped without being walked, symlinked
directories are not followed, and patterns that visit more than 50,000 files and
directories are refused. Generated and dependency directories, binary files, files
over 240,000 characters and secret-looking files (.env, *.pem, *secret*, ...) are
skipped. Token-shaped secrets in the files, --state, stdin and the questions are
scrubbed before anything is sent; a file whose path holds one is skipped, and a
question id or choice option holding one is refused. An @file of questions may sit
outside the repository (at most 64,000 bytes, not secret-named). `skipped` reports a count, counts by reason and up to 10
sample paths.

Exit codes: 0 answers printed (JSON on stdout); 2 bad questions or arguments,
or no usable file; 3 Jev unavailable (off, no API key, or every call failed),
in which case read the files instead.
"""
import argparse
import concurrent.futures
import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_client  # noqa: E402

GUARD_PATH = Path(__file__).resolve().parent / "jev-guard.py"
QUESTION_TYPES = ("noul", "choice", "score")
MAX_QUESTIONS = 32
MAX_CHOICE_OPTIONS = 255
OTHER_KEYS = ("other", "none", "none_of_the_above")
# Jev reads at most 64k tokens of state plus questions; about 4 characters per token,
# with 4k tokens kept for the questions.
TOKEN_BUDGET = 64_000
STATE_TOKEN_BUDGET = TOKEN_BUDGET - 4_000
CHARS_PER_TOKEN = 4
MAX_FILE_CHARS = STATE_TOKEN_BUDGET * CHARS_PER_TOKEN
MAX_STATE_TEXT_CHARS = 8_000
MAX_COMBINED_FILES = 20
MAX_EACH_FILES = 255
WORKERS = 8
SKIP_DIRS = {".git", "node_modules", "dist", "build", "coverage", "__pycache__", ".venv", "venv",
             ".tox", ".mypy_cache", ".pytest_cache", ".next", "target"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tgz",
                 ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".lock", ".pyc"}
GLOB_CHARS = set("*?[")
MAX_WALK_ENTRIES = 50_000
MAX_PATTERN_CHARS = 256
MAX_QUESTIONS_BYTES = 64_000
SKIPPED_SAMPLE = 10


class UsageError(Exception):
    """Bad questions, arguments or files: exit 2 with the message."""


def load_guard():
    """jev-guard.py, for its secret file patterns and text scrubber (one source of truth)."""
    spec = importlib.util.spec_from_file_location("jev_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_questions(raw, cwd, guard):
    """The question JSON text: raw itself, or an @file's text. The file may sit outside
    the repository (e.g. a scratch directory), but a secret-looking name is refused and
    only MAX_QUESTIONS_BYTES are read."""
    if not raw.startswith("@"):
        return raw
    path = Path(cwd) / raw[1:]
    try:
        resolved = path.resolve(strict=True)
        # The given path and the target's name, not the cwd's ancestors.
        names = [p.lower() for p in Path(raw[1:]).parts + (resolved.name,)]
        if any(fnmatch.fnmatch(name, pattern) for name in names for pattern in guard.REDACT_FILE_PATTERNS):
            raise UsageError("questions file has a secret-looking name; not read")
        if not resolved.is_file():
            raise UsageError("questions file is not a regular file")
        with open(resolved, "rb") as handle:
            data = handle.read(MAX_QUESTIONS_BYTES + 1)
    except (OSError, RuntimeError) as exc:
        raise UsageError(f"cannot read questions file: {getattr(exc, 'strerror', None) or exc}")
    if len(data) > MAX_QUESTIONS_BYTES:
        raise UsageError(f"questions file is over {MAX_QUESTIONS_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise UsageError("questions file is not UTF-8")


def parse_questions(raw):
    """Validate the question JSON text and return a normalized copy for Jev."""
    try:
        block = json.loads(raw)
    except ValueError as exc:
        raise UsageError(f"questions are not valid JSON: {exc}")
    if not isinstance(block, dict) or not block:
        raise UsageError("questions must be a non-empty JSON object keyed by question id")
    if len(block) > MAX_QUESTIONS:
        raise UsageError(f"at most {MAX_QUESTIONS} questions per call")
    questions = {}
    for qid, question in block.items():
        where = f"question {qid!r}"
        if not qid.strip():
            raise UsageError("question ids must be non-empty")
        if not isinstance(question, dict):
            raise UsageError(f"{where} must be an object with type and instructions")
        kind = question.get("type")
        if kind not in QUESTION_TYPES:
            raise UsageError(f"{where}: type must be one of {', '.join(QUESTION_TYPES)}")
        instructions = question.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise UsageError(f"{where}: instructions must be a non-empty string")
        criteria = question.get("criteria")
        normalized = {"type": kind, "instructions": instructions}
        if kind == "noul":
            if criteria is not None:
                if (not isinstance(criteria, dict) or set(criteria) - {"true", "false"}
                        or not all(isinstance(v, str) for v in criteria.values())):
                    raise UsageError(f"{where}: noul criteria must be {{\"true\": text, \"false\": text}}")
                normalized["criteria"] = criteria
        elif kind == "choice":
            if (not isinstance(criteria, dict) or not criteria
                    or not all(isinstance(k, str) and k.strip() for k in criteria)
                    or not all(v is None or isinstance(v, str) for v in criteria.values())):
                raise UsageError(f"{where}: choice criteria must map each option to a description or null")
            options = dict(criteria)
            if not any(k.lower() in OTHER_KEYS for k in options):
                options["other"] = "None of the above"
            if len(options) > MAX_CHOICE_OPTIONS:
                raise UsageError(f"{where}: at most {MAX_CHOICE_OPTIONS - 1} options plus `other`")
            normalized["criteria"] = options
        else:
            if (not isinstance(criteria, list) or not 2 <= len(criteria) <= 10
                    or not all(isinstance(level, str) and level.strip() for level in criteria)):
                raise UsageError(f"{where}: score criteria must be a list of 2-10 level descriptions")
            normalized["criteria"] = criteria
        questions[qid] = normalized
    return questions


def scrub_questions(questions, guard):
    """Questions with the scrubber applied to every text sent. Ids and choice keys come
    back in the answers, so one holding a secret is refused instead of rewritten."""
    scrubbed = {}
    for qid, question in questions.items():
        keys = [qid] + list(question["criteria"] if isinstance(question.get("criteria"), dict) else ())
        if any(guard._scrub(key) != key for key in keys):
            raise UsageError(f"question {qid[:40]!r}: an id or choice option looks like a secret")
        clean = dict(question, instructions=guard._scrub(question["instructions"]))
        criteria = question.get("criteria")
        if isinstance(criteria, dict):
            clean["criteria"] = {k: v if v is None else guard._scrub(v) for k, v in criteria.items()}
        elif isinstance(criteria, list):
            clean["criteria"] = [guard._scrub(level) for level in criteria]
        scrubbed[qid] = clean
    return scrubbed


def repo_root(cwd):
    """The git work tree's resolved root, or None outside a git repository."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class GlobPattern:
    """A glob matched by stepping the set of pattern positions through the path, so a
    match costs at most len(path) * len(pattern) steps whatever the stars (a regex
    backtracks exponentially on repeated `**/` or `*`)."""
    WILD = ("star", "globstar", "dirstar")

    def __init__(self, tokens):
        self.tokens = tokens

    def _close(self, states, at_segment_start):
        # Wildcards may match nothing, so a position on one also stands past it; `**/`
        # only at a segment start, so it never ends inside a name.
        out, stack = set(), list(states)
        while stack:
            t = stack.pop()
            if t not in out:
                out.add(t)
                if t < len(self.tokens) and self.tokens[t][0] in self.WILD and (
                        at_segment_start or self.tokens[t][0] != "dirstar"):
                    stack.append(t + 1)
        return out

    def fullmatch(self, text):
        end = len(self.tokens)
        states = self._close({0}, True)
        for ch in text:
            nxt = set()
            for t in states:
                if t == end:
                    continue
                kind, arg = self.tokens[t]
                if kind == "lit":
                    if ch == arg:
                        nxt.add(t + 1)
                elif kind == "one":
                    if ch != "/":
                        nxt.add(t + 1)
                elif kind == "class":
                    if arg.fullmatch(ch):
                        nxt.add(t + 1)
                elif kind == "star":
                    if ch != "/":
                        nxt.add(t)
                elif kind == "globstar":
                    nxt.add(t)
                else:  # dirstar: whole directories, so it can end only after a slash
                    nxt.add(t)
                    if ch == "/":
                        nxt.add(t + 1)
            if not nxt:
                return False
            states = self._close(nxt, ch == "/")
        return end in states


def translate(pattern):
    """A GlobPattern for a posix relative path: `**/` is zero or more directories,
    `**` (or any longer run of stars) anything, `*` anything but a slash, `?` one
    non-slash character, `[...]` a character class as in fnmatch."""
    if len(pattern) > MAX_PATTERN_CHARS:
        raise UsageError(f"glob is over {MAX_PATTERN_CHARS} characters")
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            if j - i == 1:
                out.append(("star", None))
                i = j
            elif j < n and pattern[j] == "/":
                out.append(("dirstar", None))
                i = j + 1
            else:
                out.append(("globstar", None))
                i = j
        elif c == "?":
            out.append(("one", None))
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(("lit", "["))
                i += 1
            else:
                stuff = pattern[i + 1:j].replace("\\", "\\\\")
                if stuff[0] == "!":
                    stuff = "^" + stuff[1:]
                elif stuff[0] in ("^", "["):
                    stuff = "\\" + stuff
                try:
                    out.append(("class", re.compile(f"[{stuff}]", re.DOTALL)))
                except re.error:
                    raise UsageError(f"bad character class in glob: {pattern[:80]}")
                i = j + 1
        else:
            out.append(("lit", c))
            i += 1
    return GlobPattern(out)


def split_pattern(pattern):
    """(literal base directory, rest) of a glob: the components before the first one
    holding a glob character, and the remainder."""
    parts = pattern.replace(os.sep, "/").split("/")
    first = next(i for i, part in enumerate(parts) if GLOB_CHARS & set(part))
    base = "/".join(parts[:first])
    if not base:
        base = "/" if pattern.startswith(("/", os.sep)) else "."
    return base, "/".join(parts[first:])


def expand(patterns, recursive, cwd, root):
    """(paths, skipped): paths (relative to cwd) for each pattern, in order, without
    duplicates, and patterns skipped without walking.

    A glob matches under its literal base directory (`**` spans directories); a
    directory gives its files, or its whole tree with recursive; anything else passes
    through so check_file can report it. A base outside root is not walked, symlinked
    directories are not followed, SKIP_DIRS are pruned, and visiting more than
    MAX_WALK_ENTRIES files in total raises UsageError.
    """
    seen, paths, skipped = set(), [], []
    visited = 0

    def add(path):
        if path not in seen:
            seen.add(path)
            paths.append(path)

    def count(entries):
        nonlocal visited
        visited += entries
        if visited > MAX_WALK_ENTRIES:
            raise UsageError(f"patterns visit more than {MAX_WALK_ENTRIES} files and directories; narrow them")

    def inside(directory):
        try:
            directory.resolve().relative_to(root)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def walk(top):
        for parent, dirs, files in os.walk(top, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            count(len(dirs) + len(files))
            for name in sorted(files):
                yield Path(parent) / name

    for pattern in patterns:
        full = Path(cwd) / pattern
        if GLOB_CHARS & set(pattern):
            base_text, rest = split_pattern(pattern)
            base = Path(cwd) / base_text
            if not base.is_dir():
                add(pattern)
                continue
            if not inside(base):
                skipped.append({"path": pattern, "reason": "outside the repository"})
                continue
            regex = translate(rest)
            matched = False
            for match in walk(base):
                if regex.fullmatch(match.relative_to(base).as_posix()):
                    matched = True
                    add(Path(os.path.relpath(match, cwd)).as_posix())
            if not matched:
                add(pattern)
        elif full.is_dir():
            if not inside(full):
                skipped.append({"path": pattern, "reason": "outside the repository"})
            elif recursive:
                for match in walk(full):
                    add(Path(os.path.relpath(match, cwd)).as_posix())
            else:
                children = []
                try:
                    for child in full.iterdir():
                        count(1)
                        children.append(child)
                except OSError as exc:
                    skipped.append({"path": pattern, "reason": f"unreadable: {exc.strerror or exc}"})
                    continue
                for child in sorted(children):
                    if child.is_file():
                        add(Path(os.path.relpath(child, cwd)).as_posix())
        else:
            add(pattern)
    return paths, skipped


def check_file(path, root, cwd, guard):
    """(display path, text) for a file safe to send, else raise UsageError with the reason."""
    full = Path(cwd) / path
    try:
        resolved = full.resolve(strict=True)
    except (OSError, RuntimeError):
        raise UsageError("not found")
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise UsageError("outside the repository")
    parts = rel.parts
    if any(part in SKIP_DIRS for part in parts[:-1]):
        raise UsageError("generated or dependency directory")
    # The given path and its target both count, so a symlink can't hide a secret's name.
    names = [p.lower() for p in parts + Path(path).parts]
    if any(fnmatch.fnmatch(name, pattern) for name in names for pattern in guard.REDACT_FILE_PATTERNS):
        raise UsageError("secret-looking file name; not sent")
    # The path itself is sent too, so a token-shaped name skips the file.
    if guard._scrub(rel.as_posix()) != rel.as_posix():
        raise UsageError("secret-looking path; not sent")
    if not resolved.is_file():
        raise UsageError("not a regular file")
    if resolved.suffix.lower() in SKIP_SUFFIXES:
        raise UsageError("binary or lock file")
    try:
        size = resolved.stat().st_size
        if size > MAX_FILE_CHARS * 4:  # UTF-8 is at most 4 bytes a character
            raise UsageError(f"too large (over {MAX_FILE_CHARS} characters)")
        data = resolved.read_bytes()
    except OSError as exc:
        raise UsageError(f"unreadable: {exc.strerror or exc}")
    if b"\0" in data[:8192]:
        raise UsageError("binary file")
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise UsageError("empty")
    if len(text) > MAX_FILE_CHARS:
        raise UsageError(f"too large (over {MAX_FILE_CHARS} characters)")
    return rel.as_posix(), guard._scrub(text)


def tokens(text):
    return -(-len(text) // CHARS_PER_TOKEN)


def split_hint(sizes, budget):
    """Greedy first-fit-decreasing groups of paths that each fit the budget."""
    bins = []
    for path, size in sorted(sizes.items(), key=lambda item: -item[1]):
        for group in bins:
            if group[0] + size <= budget:
                group[0] += size
                group[1].append(path)
                break
        else:
            bins.append([size, [path]])
    return [group[1] for group in bins]


def gather(args, cwd, root, guard, limit):
    files = {}
    paths, skipped = expand(args.paths, args.recursive, cwd, root)
    for path in paths:
        # The cap comes first, so a path past it is never read.
        if len(files) >= limit:
            skipped.append({"path": path, "reason": f"over the {limit}-file cap; narrow the pattern"
                            + ("" if args.each else " or use --each")})
            continue
        try:
            name, text = check_file(path, root, cwd, guard)
        except UsageError as exc:
            skipped.append({"path": path, "reason": str(exc)})
            continue
        files.setdefault(name, text)
    return files, skipped


def summarize(skipped):
    """{count, by_reason, sample} for a list of {path, reason}."""
    by_reason = {}
    for item in skipped:
        by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1
    return {"count": len(skipped), "by_reason": by_reason, "sample": skipped[:SKIPPED_SAMPLE]}


def add_usage(total, spent):
    total["calls"] += 1
    if spent:
        total["input_tokens"] += spent["input_tokens"]
        total["output_tokens"] += spent["output_tokens"]
        total["usd"] = round(total["usd"] + spent["usd"], 9)
    else:
        total["unknown_cost_calls"] += 1


def run(args, cfg, classify_fn=None, cwd=None):
    """(exit code, output dict, log entry) for parsed args."""
    cwd = cwd or os.getcwd()
    if not jev_client.feature_enabled(cfg, "ask"):
        return 3, {"error": "Jev is off (relay/config.json jev.enabled and jev.features.ask); "
                            "read the files instead"}, None
    if not jev_client.api_key(cfg):
        return 3, {"error": "no Jev API key (TYPESAFE_API_KEY); read the files instead"}, None
    if args.each and (args.state is not None or args.stdin):
        raise UsageError("--state and --stdin go with the default single call, not --each")
    if not args.paths and args.state is None and not args.stdin:
        raise UsageError("give at least one path, --state or --stdin")
    if args.state is not None and len(args.state) > MAX_STATE_TEXT_CHARS:
        raise UsageError(f"--state is over {MAX_STATE_TEXT_CHARS} characters; pass paths or --stdin instead")
    guard = load_guard()
    root = repo_root(cwd)
    if root is None:
        raise UsageError("not inside a git repository")
    questions = scrub_questions(parse_questions(read_questions(args.questions, cwd, guard)), guard)
    state_text = None if args.state is None else guard._scrub(args.state)
    files, skipped = gather(args, cwd, root, guard, MAX_EACH_FILES if args.each else MAX_COMBINED_FILES)
    if args.paths and not files and not (args.state or args.stdin):
        summary = summarize(skipped)
        raise UsageError(
            f"no usable file: {summary['count']} skipped ("
            + ", ".join(f"{reason}: {n}" for reason, n in summary["by_reason"].items()) + ")"
            + "".join(f"; {s['path']}: {s['reason']}" for s in summary["sample"]))
    question_tokens = tokens(json.dumps(questions))
    if question_tokens > TOKEN_BUDGET - STATE_TOKEN_BUDGET:
        raise UsageError("questions are too long; shorten instructions or split them")
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "unknown_cost_calls": 0}
    errors = []
    timeout_cfg = dict(cfg, timeout_seconds=cfg.get("ask_timeout_seconds"))

    def ask(state):
        spent, call_errors = {}, []
        answers = jev_client.ask(timeout_cfg, "ask", state, questions, classify_fn, call_errors,
                                 usage=spent, timeout_cap=jev_client.MAX_ASK_DEADLINE_SECONDS)
        return answers, spent, call_errors

    if args.each:
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            outcomes = list(pool.map(lambda item: ask({"path": item[0], "content": item[1]}),
                                     files.items()))
        for path, (answers, spent, call_errors) in zip(files, outcomes):
            if answers is None:
                errors.extend(call_errors)
                skipped.append({"path": path, "reason": "jev call failed: " + (",".join(call_errors) or "no answer")})
                continue
            add_usage(usage, spent)
            results.append({"path": path, "answers": answers})
        output = {"results": results, "root": str(root), "skipped": summarize(skipped), "usage": usage}
        code = 0 if results else 3
    else:
        state = {}
        if files:
            state["files"] = files
        if state_text is not None:
            state["text"] = state_text
        if args.stdin:
            limit = MAX_FILE_CHARS * 4 + 1  # UTF-8 is at most 4 bytes a character
            data = sys.stdin.buffer.read(limit)
            if len(data) >= limit:
                raise UsageError(f"stdin is over {MAX_FILE_CHARS} characters")
            text = data.decode("utf-8", errors="replace")
            if len(text) > MAX_FILE_CHARS:
                raise UsageError(f"stdin is over {MAX_FILE_CHARS} characters")
            state["input"] = guard._scrub(text)
        sizes = {path: tokens(text) for path, text in files.items()}
        extra = sum(tokens(state.get(k, "")) for k in ("text", "input"))
        if sum(sizes.values()) + extra > STATE_TOKEN_BUDGET:
            groups = split_hint(sizes, max(STATE_TOKEN_BUDGET - extra, 1))
            raise UsageError(
                f"state is about {sum(sizes.values()) + extra} tokens, over the {STATE_TOKEN_BUDGET} budget. "
                f"Split into {len(groups)} calls with the same questions: "
                + " | ".join(f"call {i}: {' '.join(group)}" for i, group in enumerate(groups, 1))
                + " (or use --each)")
        answers, spent, call_errors = ask(state)
        errors.extend(call_errors)
        if answers is None:
            output = {"error": "Jev call failed (" + (",".join(call_errors) or "no answer")
                               + "); read the files instead", "skipped": summarize(skipped)}
            code = 3
        else:
            add_usage(usage, spent)
            output = {"answers": answers, "files": sorted(files), "root": str(root),
                      "skipped": summarize(skipped), "usage": usage}
            code = 0
    decision = ("unavailable" if code else "partial" if args.each and len(output["results"]) < len(files)
                else "answered")
    entry = {"feature": "ask", "decision": decision, "mode": "each" if args.each else "combined",
             "files": len(files), "skipped": len(skipped), "questions": len(questions)}
    if errors:
        entry["errors"] = sorted(set(errors))
    return code, output, entry


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ask-jev.py", description="Ask Jev typed questions about files without reading them.",
        epilog="See the module docstring (python3 ask-jev.py --help-questions) for the question format.")
    parser.add_argument("paths", nargs="*", help="files, directories or globs (** spans directories)")
    parser.add_argument("-q", "--questions", help="JSON question object, or @file")
    parser.add_argument("--each", action="store_true", help="one call per file instead of one combined call")
    parser.add_argument("--recursive", action="store_true", help="directories include their whole tree")
    parser.add_argument("--state", help=f"short extra context (at most {MAX_STATE_TEXT_CHARS} characters)")
    parser.add_argument("--stdin", action="store_true", help="send stdin (e.g. test output) as `input`")
    parser.add_argument("--help-questions", action="store_true", help="print the question format and exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.help_questions:
        print(__doc__)
        return 0
    if not args.questions:
        print(json.dumps({"error": "missing -q/--questions (see --help-questions)"}))
        return 2
    cfg = jev_client.load_config()
    start = time.monotonic()
    try:
        code, output, entry = run(args, cfg)
    except UsageError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    if entry is not None:
        jev_client.write_log(cfg, {"ts": jev_client.timestamp(), **entry,
                                   "latency_ms": jev_client.elapsed_ms(start)})
    print(json.dumps(output, ensure_ascii=True, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
