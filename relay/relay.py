#!/usr/bin/env python3
"""Relay — context gauge + automatic session rollover for Claude Code.

Subcommands:
  prompt    UserPromptSubmit hook. Injects a context gauge (amber/red zones add
            rollover directives) or, when the prompt carries `relay:<id>`,
            injects that handoff so a fresh session continues the work.
  stop      Stop hook. In the red zone, blocks the stop ONCE per session so the
            model writes a handoff and rolls over instead of idling on a full context.
  handoff   Called by the model: stores the handoff (stdin), then either asks the
            shared VS Code bridge for a new Claude tab (rollover "open") or only
            copies the relay prompt to the clipboard (rollover "copy"). Printing
            the prompt is the fallback whenever a launch cannot be confirmed.
  status    Print the gauge for a transcript (debugging).

Hooks must never break a prompt: every hook path swallows errors and exits 0.
"""
import csv
import ctypes
import json
import locale
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = (Path(os.environ["CLAUDE_HOME"]).expanduser() / "relay"
        if os.environ.get("CLAUDE_HOME") else Path(__file__).resolve().parent)
CLAUDE_HOME = ROOT.parent
HANDOFFS = ROOT / "handoffs"
STATE = ROOT / "state"

sys.path.insert(0, str(CLAUDE_HOME / "bin"))
try:
    import jev_client
except Exception:
    jev_client = None
JEV_CLASSIFY = None  # test hook: injected as ask()'s classify_fn when set
DEFAULTS = {
    "soft_tokens": 150_000,      # amber: delegate harder, roll over at the next boundary
    "hard_tokens": 250_000,      # red: roll over now
    "task_shift_min_tokens": 30_000,  # below this a task change just continues here
    "handoff_ttl_hours": 72,
    "auto_open": True,           # legacy: false means rollover "copy"
}
ROLLOVER_MODES = ("open", "copy")
RELAY_RE = re.compile(r"\brelay:([a-f0-9]{8})\b")
MAX_TRANSCRIPT_BYTES = 8 << 20
URI_SCHEMES = {
    "com.microsoft.VSCode": "vscode",
    "com.microsoft.VSCodeInsiders": "vscode-insiders",
    "com.todesktop.230313mzl4w4u92": "cursor",
    "com.exafunction.windsurf": "windsurf",
}


def editor_scheme():
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") != "claude-vscode":
        return None
    scheme = URI_SCHEMES.get(os.environ.get("__CFBundleIdentifier", ""))
    if sys.platform == "win32" and not scheme:
        requested = os.environ.get("CLAUDE_RELAY_IDE_SCHEME", "vscode")
        scheme = requested if requested in URI_SCHEMES.values() else "vscode"
    return scheme


def open_editor_prompt(next_prompt):
    scheme = editor_scheme()
    if not scheme or sys.platform not in ("darwin", "win32"):
        return False
    uri = f"{scheme}://anthropic.claude-code/open?prompt={urllib.parse.quote(next_prompt)}"
    try:
        if sys.platform == "win32":
            os.startfile(uri)
        else:
            result = subprocess.run(["open", uri], check=False)
            if result.returncode != 0:
                raise OSError(f"open exited {result.returncode}")
    except OSError as exc:
        print(f"could not request editor session ({exc}); falling back to relay prompt.")
        return False
    print("editor session launch requested with the relay prompt pre-filled; press Enter there to continue.")
    print(f"If no tab appears, open a Claude tab and send: {next_prompt}")
    return True


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def context_tokens(transcript_path):
    """Scan backward in 1 MiB chunks, reading at most 8 MiB total."""
    try:
        with open(transcript_path, "rb") as f:
            cursor = os.fstat(f.fileno()).st_size
            remaining = MAX_TRANSCRIPT_BYTES
            partial = b""
            while cursor > 0 and remaining > 0:
                amount = min(cursor, remaining, 1 << 20)
                cursor -= amount
                remaining -= amount
                f.seek(cursor)
                lines = (f.read(amount) + partial).split(b"\n")
                # Carry the oldest partial record into the next chunk. Discard it
                # if the scan budget is exhausted; a JSON-looking suffix is unsafe.
                partial = lines.pop(0) if cursor else b""
                for line in reversed(lines):
                    if b'"usage"' not in line and b'"compact_boundary"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(entry, dict) or entry.get("isSidechain"):
                        continue
                    if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                        return None
                    if entry.get("type") != "assistant":
                        continue
                    message = entry.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if not isinstance(usage, dict) or "input_tokens" not in usage:
                        continue
                    counts = [usage.get(key, 0) for key in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
                    if any(type(count) is not int or count < 0 for count in counts):
                        return None
                    return sum(counts)
    except OSError:
        return None
    return None


def zone(tokens, cfg):
    if tokens is None:
        return "unknown"
    if tokens >= cfg["hard_tokens"]:
        return "red"
    if tokens >= cfg["soft_tokens"]:
        return "amber"
    return "green"


def k(n):
    return f"{round(n / 1000)}k"


def load_state(session_id):
    try:
        return json.loads((STATE / f"{session_id}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _restrict_windows_state(path):
    """Remove inherited access from a newly created, still-empty staging path."""
    system_dir = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(system_dir, len(system_dir))
    if not 0 < length < len(system_dir):
        raise OSError("could not locate the Windows system directory")
    system_dir = Path(system_dir.value)
    identity = subprocess.run([str(system_dir / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, check=True)
    rows = list(csv.reader(identity.stdout.splitlines()))
    sid = rows[0][-1].strip() if rows and rows[0] else ""
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise OSError("could not determine the current Windows user SID")
    subprocess.run([str(system_dir / "icacls.exe"), str(path), "/inheritance:r",
                    "/grant:r", f"*{sid}:F"],
                   capture_output=True, text=True, check=True)


def save_state(session_id, state):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{session_id}.json"
    stage = STATE / f".private-{secrets.token_hex(8)}"
    if os.name == "nt":
        stage.mkdir()
        try:
            _restrict_windows_state(stage)
        except Exception:
            stage.rmdir()
            raise
        tmp = stage / "state.tmp"
    else:
        tmp = path.with_name(path.name + f".{secrets.token_hex(8)}.tmp")
    # State may hold recent prompts / the handoff goal (only when "shift" is enabled,
    # see record_prompt/cmd_prompt). Restrict access before writing any of that text.
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if os.name == "nt":
            _restrict_windows_state(tmp)
        elif hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(json.dumps(state))
        tmp.replace(path)
    finally:
        if fd is not None:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        if os.name == "nt":
            stage.rmdir()


def sweep(cfg):
    cutoff = time.time() - cfg["handoff_ttl_hours"] * 3600
    for folder in (HANDOFFS, STATE):
        for p in folder.glob("*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


HOW = ("To roll over: write GOAL, STATE, DECISIONS & CONSTRAINTS, FILES, VERIFIED vs UNVERIFIED, "
       "NEXT STEP and NEXT PROMPT; pipe the handoff to "
       f"`python3 {shlex.quote(str(Path(__file__).resolve()))} handoff --title \"<short title>\"` "
       "(body on stdin via quoted heredoc). Report the script's actual result; if it only saves/copies "
       "a relay prompt, tell the user how to start the new session. Then stop here.")


def emit_context(text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": text}}))


def read_handoff(path):
    data = path.read_bytes()
    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError:
        # Before UTF-8 was explicit, Windows handoffs used the local code page.
        encoding = (locale.getencoding() if hasattr(locale, "getencoding")
                    else locale.getpreferredencoding(False))
        body = data.decode(encoding)
    # Match the universal-newline behavior used by the original text reader.
    return body.replace("\r\n", "\n").replace("\r", "\n")


def _shift_enabled():
    """Whether the opt-in "shift" jev feature is on — the only reason relay persists
    prompt text or the handoff goal to session state. False (never raises) when
    jev_client failed to import or the config can't be loaded."""
    if jev_client is None:
        return False
    try:
        return jev_client.feature_enabled(jev_client.load_config(ROOT / "config.json"), "shift")
    except Exception:
        return False


def record_prompt(session_id, prompt):
    """Append prompt (truncated) to state["recent_prompts"], keeping the last 5.

    No-op unless "shift" is enabled: this is the only feature that reads
    recent_prompts, so prompt text is never persisted otherwise."""
    if not _shift_enabled():
        return
    try:
        state = load_state(session_id)
        recent = state.get("recent_prompts")
        recent = list(recent) if isinstance(recent, list) else []
        recent.append(prompt[:500])
        state["recent_prompts"] = recent[-5:]
        save_state(session_id, state)
    except Exception:
        pass


def classify_shift(jcfg, session_id, prompt, z):
    """Ask jev whether prompt continues the session's recent work.

    Returns (sentence_override, log_entry) where sentence_override is:
      - a replacement string for the generic shift sentence (strong shift)
      - "" to drop the shift sentence entirely (strong continuation)
      - None to leave the default sentence unchanged (no opinion / mid confidence)
    """
    state = load_state(session_id)
    prev = state.get("recent_prompts") if isinstance(state.get("recent_prompts"), list) else []
    goal = state.get("handoff_goal") or ""
    if not prev and not goal:
        return None, None
    start = time.monotonic()
    answers = jev_client.ask(jcfg, "shift", {
        "recent_prompts": prev, "task_goal": goal, "new_prompt": prompt[:2000],
    }, {"continues": {"type": "noul", "instructions": (
        "The new prompt continues the same task as the recent prompts or task goal (a follow-up, "
        "correction, or next step) rather than starting unrelated work.")}}, JEV_CLASSIFY)
    latency = jev_client.elapsed_ms(start)
    p = jev_client.noul(answers, "continues")
    if p is not None and p < jcfg["shift_low"]:
        sentence, decision = (
            f"TASK SHIFT DETECTED (jev p={p:.2f} that this continues the current task): this prompt "
            "appears to start unrelated work. Do not do it here — roll over now with this prompt "
            "copied verbatim under NEXT PROMPT."), "shift"
    elif p is not None and p > jcfg["shift_high"]:
        sentence, decision = "", "continue"
    else:
        sentence, decision = None, "unsure"
    log = {"ts": jev_client.timestamp(), "feature": "shift", "zone": z,
           "p": p, "decision": decision, "latency_ms": latency}
    return sentence, log


def cmd_prompt():
    data = json.load(sys.stdin)
    cfg = config()
    prompt = data.get("prompt") or ""
    session_id = data.get("session_id") or "unknown"
    shift_on = _shift_enabled()

    m = RELAY_RE.search(prompt)
    if m:
        path = HANDOFFS / f"{m.group(1)}.md"
        if path.exists():
            try:
                body = read_handoff(path)
            except UnicodeDecodeError:
                emit_context(f"[relay] Handoff {m.group(1)} could not be decoded as UTF-8 or the "
                             "local legacy encoding. Ask the user to convert that handoff file "
                             "to UTF-8 using its original encoding, then retry. Do not infer its contents.")
                return
            if shift_on:
                try:
                    state = load_state(session_id)
                    for line in body.splitlines():
                        if line.strip().startswith("GOAL:"):
                            state["handoff_goal"] = line.strip()[:500]
                            break
                    save_state(session_id, state)
                except Exception:
                    pass
            emit_context(
                "[relay] This session CONTINUES earlier work. The previous session's handoff follows; "
                "treat it as your working memory. Re-verify anything it lists as UNVERIFIED before relying "
                "on it. If it has a NEXT PROMPT section, that is the user's actual request — act on it now.\n\n"
                + body)
            os.utime(path)
        else:
            emit_context(f"[relay] Handoff {m.group(1)} was not found (expired or deleted). Tell the user.")
        return

    tokens = context_tokens(data.get("transcript_path") or "")
    z = zone(tokens, cfg)
    if z == "unknown":
        if data.get("transcript_path"):
            emit_context("[relay] Context usage is unknown; no recent main-thread usage was available "
                         "within the 8 MiB scan limit. Do not infer a zone or force rollover.")
        record_prompt(session_id, prompt)
        return
    if tokens < cfg["task_shift_min_tokens"]:
        record_prompt(session_id, prompt)
        return  # fresh session: zero overhead
    if load_state(session_id).get("handoff_done"):
        emit_context(f"[relay] context ~{k(tokens)}. This session was already handed off — if the user is "
                     "still prompting here, answer briefly and remind them the work continues in the new session.")
        record_prompt(session_id, prompt)
        return

    shift = ("TASK-SHIFT RULE: if this prompt starts work unrelated to what this session has been doing, "
             "do not do it here — roll over with the prompt copied verbatim under NEXT PROMPT.")
    if z in ("green", "amber") and jev_client is not None:
        try:
            jcfg = jev_client.load_config(ROOT / "config.json")
            if jev_client.feature_enabled(jcfg, "shift"):
                override, log = classify_shift(jcfg, session_id, prompt, z)
                if override is not None:
                    shift = override
                if log:
                    jev_client.write_log(jcfg, log)
        except Exception:
            pass

    gauge = f"[relay] context ~{k(tokens)} tokens — {z.upper()} (amber {k(cfg['soft_tokens'])}, red {k(cfg['hard_tokens'])})."
    if z == "green":
        emit_context(" ".join(part for part in (gauge, shift, HOW) if part))
    elif z == "amber":
        heavy = ("Context is heavy: route ALL read-heavy or mechanical work through scout/runner/builder "
                 "subagents so their output stays out of this context, and roll over at the next natural "
                 "boundary (unit of work finished, tests green).")
        emit_context(" ".join(part for part in (gauge, heavy, shift, HOW) if part))
    else:
        emit_context(f"{gauge} ROLL OVER NOW: do no new work in this session. Write the handoff, copy this prompt "
                     f"verbatim under NEXT PROMPT, and open the new session. {HOW}")
    record_prompt(session_id, prompt)


def cmd_stop():
    data = json.load(sys.stdin)
    if data.get("stop_hook_active"):
        return
    cfg = config()
    session_id = data.get("session_id") or "unknown"
    tokens = context_tokens(data.get("transcript_path") or "")
    if zone(tokens, cfg) != "red":
        return
    state = load_state(session_id)
    if state.get("handoff_done") or state.get("stop_nudged"):
        return
    state["stop_nudged"] = True
    save_state(session_id, state)
    print(json.dumps({"decision": "block", "reason": (
        f"[relay] Context is at ~{k(tokens)} tokens (red ≥ {k(cfg['hard_tokens'])}). Before stopping, roll this "
        f"session over so the next prompt starts fresh. {HOW} If work is mid-flight, the handoff's NEXT STEP must "
        "say exactly where to resume.")}))


def git(cwd, *args):
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def rollover_mode(cfg):
    """CLAUDE_RELAY_ROLLOVER wins, then config "rollover", then legacy auto_open."""
    mode = os.environ.get("CLAUDE_RELAY_ROLLOVER", "").strip().lower()
    if mode in ROLLOVER_MODES:
        return mode
    if cfg.get("rollover") in ROLLOVER_MODES:
        return cfg["rollover"]
    return "open" if cfg.get("auto_open", True) else "copy"


def clipboard_commands():
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    return [[shutil.which(c[0])] + c[1:] for c in candidates if shutil.which(c[0])]


def copy_to_clipboard(text):
    for command in clipboard_commands():
        try:
            result = subprocess.run(command, input=text, text=True, encoding="utf-8",
                                    capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


def print_copy_instructions(next_prompt):
    if copy_to_clipboard(next_prompt):
        print(f"relay prompt copied to the clipboard: {next_prompt}")
        print("tell the user: start a new Claude session (new tab or /clear) and paste it.")
    else:
        print("clipboard unavailable; tell the user: start a new Claude session "
              f"(new tab or /clear) and send:\n  {next_prompt}")


HANDOFF_SECTIONS = ("GOAL", "STATE", "NEXT STEP", "VERIFIED")
SECTION_RE = {name: re.compile(r"^[#*\-\s]*" + re.escape(name) + r"\b", re.IGNORECASE)
              for name in HANDOFF_SECTIONS}


def handoff_gaps(body, next_step_concrete, verified_backed):
    lines = body.splitlines()
    gaps = [f"missing {name}" for name in HANDOFF_SECTIONS
            if not any(SECTION_RE[name].match(line) for line in lines)]
    if next_step_concrete is not None and next_step_concrete < 0.5:
        gaps.append("NEXT STEP is not a concrete action")
    if verified_backed is not None and verified_backed < 0.5:
        gaps.append("VERIFIED claims do not cite commands/exit codes")
    return gaps


def grade_handoff(jcfg, body):
    """Ask jev to grade the handoff. Returns (score_or_None, gaps, confidence, latency_ms)."""
    questions = {
        "actionable": {"type": "score", "instructions": (
            "How actionable is this handoff for a fresh session that has no other context?"),
            "criteria": [
                "unusable: no clear goal or next step",
                "vague: goal present but state or next step unclear",
                "workable: goal and next step clear, gaps in files or verification",
                "solid: concrete next step, file paths, verification mostly backed by commands",
                "excellent: a fresh session could act immediately; verified claims cite commands and exit codes",
            ]},
        "next_step_concrete": {"type": "noul", "instructions": (
            "NEXT STEP names one exact, concrete action (a command, file, or edit) a fresh session "
            "can take immediately.")},
        "verified_backed": {"type": "noul", "instructions": (
            "Claims listed as VERIFIED cite the actual commands run and their results or exit codes.")},
    }
    start = time.monotonic()
    answers = jev_client.ask(jcfg, "handoff_grade", {"handoff": body[:12000]}, questions, JEV_CLASSIFY)
    latency = jev_client.elapsed_ms(start)
    try:
        score = float(answers["actionable"]["score"])
        confidence = answers["actionable"].get("confidence")
    except Exception:
        return None, [], None, latency
    gaps = handoff_gaps(body, jev_client.noul(answers, "next_step_concrete"),
                        jev_client.noul(answers, "verified_backed"))
    return score, gaps, confidence, latency


def cmd_handoff(argv):
    title, do_open, accept_weak = "continue", True, False
    it = iter(argv)
    for a in it:
        if a == "--title":
            title = next(it, title)
        elif a == "--no-open":
            do_open = False
        elif a == "--accept-weak":
            accept_weak = True
    body = sys.stdin.read().strip()
    if len(body) < 40:
        sys.exit("relay: handoff body is empty/too short — pipe the handoff on stdin.")
    if jev_client is not None:
        try:
            jcfg = jev_client.load_config(ROOT / "config.json")
            if jev_client.feature_enabled(jcfg, "handoff_grade"):
                score, gaps, confidence, latency = grade_handoff(jcfg, body)
                weak = score is not None and score < jcfg["handoff_min_score"]
                if score is not None:
                    entry = {"ts": jev_client.timestamp(), "feature": "handoff_grade", "score": score,
                             "confidence": confidence, "weak": weak, "latency_ms": latency}
                    if accept_weak:
                        entry["accepted_weak"] = True
                    jev_client.write_log(jcfg, entry)
                if weak and not accept_weak:
                    print(f"relay: handoff looks weak (jev score {score:.1f}/4, need {jcfg['handoff_min_score']}): "
                          + "; ".join(gaps or ["too vague for a fresh session"])
                          + ". Fix the handoff and rerun, or pass --accept-weak to save it anyway.")
                    sys.exit(3)
        except SystemExit:
            raise
        except Exception:
            pass
    cfg = config()
    sweep(cfg)
    hid = secrets.token_hex(4)
    cwd = os.getcwd()
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")
    dirty = git(cwd, "status", "--porcelain")
    header = "\n".join([
        f"# Relay handoff {hid}: {title}",
        f"- from session: {session_id}",
        f"- cwd: {cwd}",
        f"- branch: {git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD') or 'n/a'} @ {git(cwd, 'rev-parse', '--short', 'HEAD') or 'n/a'}",
        f"- uncommitted files at handoff: {len(dirty.splitlines()) if dirty else 0}",
        f"- written: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
    ])
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    path = HANDOFFS / f"{hid}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(header + "\n" + body + "\n", encoding="utf-8")
    tmp.replace(path)
    if session_id != "unknown":
        state = load_state(session_id)
        state["handoff_done"] = hid
        save_state(session_id, state)

    next_prompt = f"relay:{hid} continue \"{title}\" from the handoff."
    print(f"handoff saved: {path}")
    do_open = do_open and rollover_mode(cfg) == "open"
    helper = CLAUDE_HOME / "bin" / "rollover-open.py"
    if do_open and helper.is_file():
        result = subprocess.run([sys.executable, str(helper), "open", "--client", "claude",
                                 "--handoff", str(path), "--resume-token", f"relay:{hid}"],
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            print(result.stdout.strip())
            return
        print(f"handoff bridge failed (exit {result.returncode}): "
              f"{result.stdout.strip()} {result.stderr.strip()}".strip())
    if do_open and open_editor_prompt(next_prompt):
        return
    print_copy_instructions(next_prompt)


def cmd_status(argv):
    cfg = config()
    tokens = context_tokens(argv[0]) if argv else None
    print(json.dumps({"tokens": tokens, "zone": zone(tokens, cfg), "rollover": rollover_mode(cfg),
                      "config": cfg}))


def main():
    # Hooks and piped handoffs use UTF-8, independent of the Windows code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd in ("prompt", "stop"):
        try:
            cmd_prompt() if cmd == "prompt" else cmd_stop()
        except Exception:
            pass
        sys.exit(0)
    if cmd == "handoff":
        return cmd_handoff(rest)
    if cmd == "status":
        return cmd_status(rest)
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
