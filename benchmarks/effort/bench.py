"""Builder and critic roles at --effort low|medium|high (or --efforts), scored on hidden tests and seeded defects.

Usage: python benchmarks/effort/bench.py --output <private-path>.json [--builder-reps 3] [--critic-reps 2]
       [--jobs 3] [--cap 12] [--only builder|critic] [--builder-model <id>] [--critic-model <id>]
       [--efforts low,medium,high]
       python benchmarks/effort/bench.py --check    (offline: validate tasks and cases, no claude calls)

Arms (override with --builder-model / --critic-model): the builder runs on `sonnet`; the critic runs on the explicit id `claude-fable-5-2`, because on
2026-11-15 `claude -p --model fable` resolved to claude-fable-5-1 and fell back to claude-opus-4-8.
Each builder task gives a stub and visible tests, then scores hidden tests (builder_tasks.py); each critic
case gives a diff and scores whether a finding lands within its `accept` range (default two lines of the seeded defect), or that a clean
change gets no findings (critic_tasks.py). Rows are rewritten to --output after every job; keep that file
private. Prompts go on stdin (claude.CMD runs through cmd.exe, which would treat < > in argv as redirects).
"""
import argparse, concurrent.futures as cf, difflib, hashlib, json, math, re, shutil, subprocess, sys, tempfile
import threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from builder_tasks import TASKS  # noqa: E402
from critic_tasks import CASES  # noqa: E402

CLAUDE = shutil.which("claude") or "claude"
EFFORTS = ("low", "medium", "high")
EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")
BUILDER_MODEL, CRITIC_MODEL = "sonnet", "claude-fable-5-2"
DEFAULT_MODELS = (BUILDER_MODEL, CRITIC_MODEL)
MODEL_ID = re.compile(r"claude-[a-z]+-[0-9]+(-[0-9]+)*")  # full versioned id; aliases cannot be checked

EXPECTED = {"builder": "claude-sonnet-5-5", "critic": "claude-fable-5-2"}
DEFAULT_EXPECTED = dict(EXPECTED)
lock, spent, rows = threading.Lock(), [0.0], []
ARGS = OUT = None
UNITTEST_TIMEOUT = 60


def model_mismatch(role, used):
    """Error text when a non-Haiku model in `used` is not the expected id for `role`, or that id is absent."""
    bad = sorted(k for k in used if "haiku" not in k and k != EXPECTED[role])
    if bad:
        return f"model mismatch: {bad}"
    if EXPECTED[role] not in used:
        return f"model mismatch: expected {EXPECTED[role]} missing, saw {sorted(used)}"
    return None


def num(x):
    """x if it is a real number (not bool), else 0, so malformed CLI fields cannot crash a paid row."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


def safe_cost(x):
    """Finite float cost, or 0.0 (the row is then labelled "bad cost")."""
    try:
        c = float(num(x))
    except OverflowError:
        return 0.0
    return c if math.isfinite(c) else 0.0


def run(prompt, model, effort, cwd, tools, role, extra=()):
    cmd = [CLAUDE, "-p", "--model", model, "--effort", effort, "--output-format", "json",
           "--max-budget-usd", "1.5", "--no-session-persistence", "--strict-mcp-config",
           "--setting-sources", "", "--tools", tools, *extra]
    t = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "secs": 900}
    dt = round(time.monotonic() - t, 1)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return {"error": (p.stderr or p.stdout)[-300:], "rc": p.returncode, "secs": dt}
    if not isinstance(d, dict):
        return {"error": f"non-object json: {p.stdout[-300:]}", "rc": p.returncode, "secs": dt}
    mu = d.get("modelUsage")
    if not isinstance(mu, dict):
        mu = {}
    result = d.get("result")
    r = {"rc": p.returncode, "secs": dt, "result": result.strip() if isinstance(result, str) else "",
         "out_tokens": sum(num(v.get("outputTokens")) for k, v in mu.items()
                           if isinstance(v, dict) and "haiku" not in k),
         "cost": safe_cost(d.get("total_cost_usd")), "models": sorted(map(str, mu)),
         "turns": d.get("num_turns")}
    if d.get("is_error"):
        r["error"] = f"cli error: {d.get('subtype')}"
    cost = d.get("total_cost_usd")
    if cost is not None and (isinstance(cost, bool) or cost != 0 and safe_cost(cost) == 0.0):
        bad = f"bad cost: {str(cost)[:40]}"
        r["error"] = f"{r['error']}; {bad}" if "error" in r else bad
    # A CLI error before the role's model answered leaves no non-Haiku usage; that is not a wrong model.
    served = any("haiku" not in k for k in mu)
    mismatch = None if d.get("is_error") and not served else model_mismatch(role, mu)
    if mismatch:
        r["error"] = f"{r['error']}; {mismatch}" if "error" in r else mismatch
    return r


def scoring_error(e, r):
    """Scoring failure text, keeping any earlier run error after it."""
    msg = f"scoring: {type(e).__name__}: {e}"
    return f"{msg}; {r['error']}" if r.get("error") else msg


def builder(task, effort):
    t = TASKS[task]
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / "solution.py").write_text(t["stub"], encoding="utf-8")
        (ws / "test_solution.py").write_text(t["visible"], encoding="utf-8")
        h = hashlib.sha256((ws / "test_solution.py").read_bytes()).hexdigest()
        prompt = (f"Edit only solution.py in the current directory. {t['spec']} test_solution.py shows "
                  "examples; do not edit it or create other files. Reply DONE when finished.")
        r = run(prompt, BUILDER_MODEL, effort, ws, "Read,Edit,Write", "builder",
                ("--permission-mode", "acceptEdits"))
        try:
            return score_builder(r, t, ws, h)
        except Exception as e:  # keep the paid run's cost
            r.update(error=scoring_error(e, r), correct=False)
            return r


def score_builder(r, t, ws, h):
    extra = sorted(p.name for p in ws.iterdir() if p.name not in
                   {"solution.py", "test_solution.py", "__pycache__"})
    tamper = hashlib.sha256((ws / "test_solution.py").read_bytes()).hexdigest() != h or bool(extra)
    try:
        vis = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_solution"], cwd=ws,
                             capture_output=True, text=True, timeout=UNITTEST_TIMEOUT)
        vis_pass = vis.returncode == 0
    except subprocess.TimeoutExpired:
        vis_pass = False
    (ws / "test_hidden.py").write_text(t["hidden"], encoding="utf-8")
    try:
        hid = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_hidden"], cwd=ws,
                             capture_output=True, text=True, timeout=UNITTEST_TIMEOUT)
        hid_pass, err = hid.returncode == 0, hid.stderr
        m = re.search(r"Ran (\d+) tests?", err)
        fails = re.search(r"FAILED \((.*)\)", err)
        summary = (fails.group(1) if fails else "ok") if m else err[-200:]
    except subprocess.TimeoutExpired:
        hid_pass, err, summary = False, "", "timeout"
    r.update(visible_pass=vis_pass, hidden_pass=hid_pass, tamper=tamper,
             extra_files=extra, hidden_summary=summary,
             hidden_tail=err[-1200:] if not hid_pass else "",
             solution=(ws / "solution.py").read_text(encoding="utf-8", errors="replace"))
    r["correct"] = r["visible_pass"] and r["hidden_pass"] and not tamper and "error" not in r
    return r


def case_diff(c):
    seeded = None
    if c["seed"]:
        hits = [i for i, line in enumerate(c["new"].splitlines(), 1) if c["seed"] in line]
        assert len(hits) == 1, ("seed must match exactly one line", hits)
        seeded = hits[0]
    diff = "".join(difflib.unified_diff(c["old"].splitlines(True), c["new"].splitlines(True),
                                        "a/" + c["file"], "b/" + c["file"]))
    return seeded, diff


def parse_line(x):
    """Line number from an int or digit-string; None for anything else (bool is rejected)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    s = x.strip() if isinstance(x, str) else ""
    if s.isascii() and s.isdigit() and len(s) <= 18:  # longer strings are not line numbers
        return int(s)
    return None


def score_critic(case, findings, seeded=None):
    """Score parsed findings: {correct, false_pos} for a clean case, {correct, extra_findings} otherwise."""
    if seeded is None and case["seed"]:
        seeded = case_diff(case)[0]
    if seeded is None:
        return {"correct": findings == [], "false_pos": len(findings) if findings else 0}
    lo, hi = case.get("accept", (seeded - 2, seeded + 2))
    lines = [n for n in (parse_line(x.get("line")) for x in (findings or []) if isinstance(x, dict))
             if n is not None]
    return {"correct": any(lo <= n <= hi for n in lines),
            "extra_findings": sum(1 for n in lines if not lo <= n <= hi)}


def critic(case, effort):
    c = CASES[case]
    seeded, diff = case_diff(c)
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / c["file"]).write_text(c["new"], encoding="utf-8")
        (ws / "change.diff").write_text(diff, encoding="utf-8")
        prompt = (f"Review the change in change.diff; {c['file']} is the file after the change. Stated intent: "
                  f"{c['intent']} Report only correctness or security defects in the changed file that would "
                  "make it behave wrongly or unsafely relative to the stated intent and its docstrings; no "
                  "style, naming or test suggestions. Reply with ONLY a JSON object "
                  f'{{"findings":[{{"file":"{c["file"]}","line":<line number in the changed file>,'
                  '"kind":"<short>","why":"<one sentence>"}]} and use {"findings":[]} if there are none.')
        r = run(prompt, CRITIC_MODEL, effort, ws, "Read,Grep,Glob", "critic")
    try:
        return score_critic_run(r, c, seeded)
    except Exception as e:  # keep the paid run's cost
        r.update(error=scoring_error(e, r), correct=False)
        return r


def score_critic_run(r, c, seeded):
    m = re.search(r"\{.*\}", r.get("result", ""), re.S)
    try:
        f = json.loads(m.group(0))["findings"] if m else None
    except (ValueError, KeyError, TypeError):
        f = None
    if not isinstance(f, list):
        f = None
    r.update(findings=f, parsed=f is not None, seeded_line=seeded)
    r.update(score_critic(c, f, seeded))
    if "error" in r or f is None:
        r["correct"] = False
    return r


def unittest_ok(ws, module):
    return subprocess.run([sys.executable, "-m", "unittest", "-q", module], cwd=ws, capture_output=True,
                          text=True, timeout=UNITTEST_TIMEOUT).returncode == 0


def check():
    bad = 0
    for name, t in TASKS.items():
        problems = []
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "test_solution.py").write_text(t["visible"], encoding="utf-8")
            (ws / "test_hidden.py").write_text(t["hidden"], encoding="utf-8")
            for label, src, want in (("stub", t["stub"], False), ("ref", t["ref"], True)):
                (ws / "solution.py").write_text(src, encoding="utf-8")
                shutil.rmtree(ws / "__pycache__", ignore_errors=True)
                for module in ("test_solution", "test_hidden"):
                    if unittest_ok(ws, module) != want:
                        problems.append(f"{label} {'fails' if want else 'passes'} {module}")
        bad += bool(problems)
        print(f"builder {name}: {'; '.join(problems) or 'ok'}")
    for name, c in CASES.items():
        problems = []
        for label in ("old", "new"):
            try:
                compile(c[label], f"{name}:{label}", "exec")
            except SyntaxError as e:
                problems.append(f"{label} does not compile: {e}")
        try:
            seeded, diff = case_diff(c)
            if not diff:
                problems.append("empty diff")
        except AssertionError as e:
            problems.append(str(e))
            seeded = None
        bad += bool(problems)
        print(f"critic {name}: {'; '.join(problems) or 'ok'} (seeded line {seeded})")
    return 1 if bad else 0


def work(job):
    role, item, effort, rep = job
    with lock:
        if spent[0] >= ARGS.cap:
            return None
    try:
        r = (builder if role == "builder" else critic)(item, effort)
    except Exception as e:  # keep the row: a crash is a failed, counted run
        r = {"error": f"exception: {type(e).__name__}: {e}", "correct": False}
    r.update(role=role, item=item, effort=effort, rep=rep)
    with lock:
        spent[0] += r.get("cost") or 0.0
        rows.append(r)
        OUT.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(json.dumps({k: r.get(k) for k in ("role", "item", "effort", "rep", "correct", "out_tokens", "secs",
                                                 "cost", "hidden_summary", "false_pos", "extra_findings",
                                                 "error")}) + f" spent=${spent[0]:.2f}", flush=True)
    return r


def set_models(builder_model=None, critic_model=None):
    """Point both roles at their models; an explicit id is also the id the run must be served by."""
    global BUILDER_MODEL, CRITIC_MODEL
    BUILDER_MODEL = builder_model or DEFAULT_MODELS[0]
    CRITIC_MODEL = critic_model or DEFAULT_MODELS[1]
    EXPECTED["builder"] = builder_model or DEFAULT_EXPECTED["builder"]
    EXPECTED["critic"] = critic_model or DEFAULT_EXPECTED["critic"]


def main(argv=None):
    global ARGS, OUT
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output", help="JSON file for result rows (required unless --check)")
    ap.add_argument("--check", action="store_true", help="validate tasks and cases offline, then exit")
    ap.add_argument("--builder-reps", type=int, default=3)
    ap.add_argument("--critic-reps", type=int, default=2)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--cap", type=float, default=12.0)
    ap.add_argument("--only", choices=("builder", "critic"))
    ap.add_argument("--builder-model", help="full model id, e.g. claude-opus-5-5 (default: sonnet alias, "
                    "expected to serve claude-sonnet-5-5)")
    ap.add_argument("--critic-model", help="full model id (default: claude-fable-5-2)")
    ap.add_argument("--efforts", default=",".join(EFFORTS),
                    help="comma-separated efforts from " + ",".join(EFFORT_CHOICES))
    ARGS = args = ap.parse_args(argv)
    if not args.check and not args.output:
        ap.error("--output is required unless --check is given")
    efforts = tuple(dict.fromkeys(e.strip() for e in args.efforts.split(",") if e.strip()))
    if not efforts or any(e not in EFFORT_CHOICES for e in efforts):
        ap.error(f"--efforts must be a comma-separated subset of {','.join(EFFORT_CHOICES)}")
    for flag in ("builder_model", "critic_model"):
        v = getattr(args, flag)
        if v is not None and not MODEL_ID.fullmatch(v):
            # Aliases can resolve to an older model or fall back silently, so they cannot be checked.
            ap.error(f"--{flag.replace('_', '-')} needs a full versioned model id such as claude-opus-5-5")
    set_models(args.builder_model, args.critic_model)
    if args.check:
        return check()
    OUT = Path(args.output)
    OUT.write_text("[]", encoding="utf-8")  # fail on an unwritable path before any paid call
    rows.clear()
    spent[0] = 0.0
    jobs = []
    if args.only != "critic":
        for i in range(args.builder_reps):
            for t in TASKS:
                for e in efforts:
                    jobs.append(("builder", t, e, i))
    if args.only != "builder":
        for i in range(args.critic_reps):
            for c in CASES:
                for e in efforts:
                    jobs.append(("critic", c, e, i))
    print(f"{len(jobs)} jobs, cap ${args.cap}", flush=True)
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(work, jobs))
    skipped = len(jobs) - len(rows)
    print(f"\nDONE rows={len(rows)} skipped_by_cap={skipped} spent=${spent[0]:.2f} "
          f"errors={sum('error' in r for r in rows)}")
    return 1 if skipped or any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
