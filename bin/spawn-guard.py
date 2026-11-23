#!/usr/bin/env python3
"""PreToolUse hook for Agent/Task: keep subagent spawns on the cheap named roles.

Two checks, both with an explicit escape hatch in the spawn prompt:

1. A `general-purpose` / `claude` spawn (or one with no subagent_type, which defaults to
   general-purpose) starts with every tool and skill loaded, about 58K tokens against about
   23K for a named role. Denied unless the prompt has a `ROLE-EXCEPTION: <reason>` line.
2. A critic on Fable is for risky work only. Denied unless the prompt has a
   `RISK: <category>` line; routine reviews pass `model: opus` (or `sonnet` per task).

Fails open: any error, unknown payload or other tool prints nothing and exits 0.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEAVY = {"", "general-purpose", "claude"}
EXCEPTION = re.compile(r"^[ \t]*ROLE-EXCEPTION:[ \t]*\S", re.MULTILINE)
RISK = re.compile(r"^[ \t]*RISK:[ \t]*\S", re.MULTILINE)

HEAVY_REASON = (
    "spawn-guard: `{kind}` starts with every tool and skill loaded (about 58K tokens against "
    "about 23K for a named role). Use a named role instead: implementer or plan/document "
    "writing -> builder (add `model: opus` for a plan), lookup or summary -> scout, exact "
    "command -> runner, review -> critic (opus; critic-risk for risky work). If this task truly "
    "needs tools no role has, add a "
    "line `ROLE-EXCEPTION: <reason>` to the prompt and spawn again."
)
FABLE_REASON = (
    "spawn-guard: a Fable critic (the `critic-risk` role) is reserved for risky work. For a routine "
    "pre-push pass use `critic` (opus); for a per-task review inside a plan set `model: sonnet` and leave out "
    "REVIEW-SCOPE. If this is risky work (security/auth, migration, data loss, concurrency, "
    "public API, irreversible) or a low-confidence conclusion, add a line "
    "`RISK: <category>` to the prompt and spawn again."
)


def frontmatter_model(name, home=ROOT, cwd=None):
    """The `model:` of a role file, lowercased, or "" when absent.

    The nearest project file (cwd, then its parents up to the git root; outside a git
    repository only the cwd) whose frontmatter `name:` matches decides, even without a
    `model:`; the user file under `home` is used only when no such project file exists.
    Deny-biased: any candidate whose frontmatter mentions "fable" anywhere returns "fable",
    so a file the parser misreads can add a Fable denial but never remove one.
    """
    home_claude = Path(home).resolve()
    paths = []
    if isinstance(cwd, str) and cwd:
        found_root = False
        start = None
        try:
            start = Path(cwd).resolve()
            for directory in (start, *start.parents):
                claude = directory / ".claude"
                if claude.resolve() != home_claude:
                    paths.append(claude / "agents" / f"{name}.md")
                if (directory / ".git").exists():
                    found_root = True
                    break  # repository root: do not look above it
        except (OSError, ValueError, RuntimeError):
            pass  # keep the candidates found so far; the user file still applies
        if not found_root:
            # outside a git repository only the cwd itself counts as the project
            cwd_file = start / ".claude" / "agents" / f"{name}.md" if start is not None else None
            paths = [path for path in paths if path == cwd_file]
    home_file = Path(home) / "agents" / f"{name}.md"
    paths.append(home_file)
    for path in paths:
        try:
            # bad bytes are replaced, not fatal, so the Fable check below still sees the text
            text = path.read_bytes().decode("utf-8-sig", errors="replace")
        except (OSError, ValueError):
            continue  # unreadable: the next candidate applies
        lines = text.split("\n")
        block = None
        if lines[0].rstrip(" \t\r") == "---":
            for index, line in enumerate(lines[1:], start=1):
                if line.rstrip(" \t\r") == "---":
                    block = "\n".join(lines[1:index])
                    break
        if block is None:
            # no valid frontmatter: a project file that mentions Fable still denies
            if path != home_file and "fable" in text.lower():
                return "fable"
            continue  # not a valid agent file: the next candidate applies
        if "fable" in block.lower():
            return "fable"  # deny-biased: any Fable mention in the frontmatter counts as Fable
        match = re.search(r"^model:[ \t]*(\S+)", block, re.MULTILINE)
        model = match.group(1).lower() if match else ""
        named = re.search(r"^name:[ \t]*(\S+)", block, re.MULTILINE)
        if path == home_file or (named and named.group(1).strip("'\"") == name):
            return model
        # a project file without a matching `name:` is not this agent: the next candidate applies
    return ""


def decide(payload, home=ROOT):
    """The deny reason for this tool call, or None to let it through."""
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    prompt = str(tool_input.get("prompt") or "")
    kind = str(tool_input.get("subagent_type") or "").strip()
    if kind in HEAVY:
        if EXCEPTION.search(prompt):
            return None
        return HEAVY_REASON.format(kind=kind or "general-purpose (no subagent_type)")
    if kind in ("critic", "critic-risk"):
        model = str(tool_input.get("model") or "").lower() or frontmatter_model(kind, home, payload.get("cwd"))
        if "fable" in model and not RISK.search(prompt):
            return FABLE_REASON
    return None


def main():
    try:
        reason = decide(json.loads(sys.stdin.buffer.read().decode("utf-8", "replace")))
    except Exception:
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
