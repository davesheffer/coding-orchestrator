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
    "command -> runner, review -> critic (with `model: opus` unless risky). If this task truly "
    "needs tools no role has, add a "
    "line `ROLE-EXCEPTION: <reason>` to the prompt and spawn again."
)
FABLE_REASON = (
    "spawn-guard: a Fable critic is reserved for risky work. For a routine pre-push pass set "
    "`model: opus`; for a per-task review inside a plan set `model: sonnet` and leave out "
    "REVIEW-SCOPE. If this is risky work (security/auth, migration, data loss, concurrency, "
    "public API, irreversible) or a low-confidence conclusion, add a line "
    "`RISK: <category>` to the prompt and spawn again."
)


def frontmatter_model(name, home=ROOT, cwd=None):
    """The `model:` of a role file (project, then user), lowercased, or "" when unreadable."""
    roots = []
    if isinstance(cwd, str) and cwd:
        roots.append(Path(cwd) / ".claude")
    roots.append(Path(home))
    for root in roots:
        try:
            text = (root / "agents" / f"{name}.md").read_text(encoding="utf-8")
        except OSError:
            continue
        block = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
        match = re.search(r"^model:[ \t]*(\S+)", block.group(1), re.MULTILINE) if block else None
        if match:
            return match.group(1).lower()
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
    if kind == "critic":
        model = str(tool_input.get("model") or "").lower() or frontmatter_model("critic", home, payload.get("cwd"))
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
