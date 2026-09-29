#!/usr/bin/env python3
"""Jev router — opt-in PreToolUse hook that picks a subagent's model tier.

Reads a Claude Code PreToolUse payload on stdin. For Agent/Task calls it asks
TypeSafe's hosted Jev classifier which tier (sonnet/opus/fable) should run the
task and, when the answer is confident enough and different, rewrites the tool
input's `model`. Moving to a stronger tier needs less confidence than moving to
a weaker one, and a task escalates when the stronger tiers together are likely
enough, because under-routing costs more than over-routing. Configuration lives under the "jev" key of
`<install>/relay/config.json`; the API key comes from TYPESAFE_API_KEY or
`jev.api_key_file`.

Fail-open: any missing key, disabled config, error, timeout or low-confidence
answer prints nothing and exits 0, leaving the call unchanged. The classifier
call runs under a hard wall-clock deadline of min(timeout_seconds, 4) seconds so
the script always finishes inside the hook's 5 s timeout.
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jev_client import (  # noqa: E402  (re-exported for callers and tests)
    AGENT_MODELS, CONFIG_PATH, DEFAULTS, LOG_PATH, MAX_DEADLINE_SECONDS, MAX_LOG_BYTES, ROOT, TIER_RANK,
    stronger_tier,
    api_key, ask, call_with_deadline, effective_timeout, elapsed_ms, endpoint_allowed, http_classify,
    load_config, timestamp, write_log)

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
INSTRUCTIONS = ("Which model tier should run this subagent task? "
                "Pick the least expensive tier that will reliably do it well; "
                "when torn between two tiers, pick the stronger one.")


def frontmatter_model(path):
    try:
        text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    except Exception:
        return None
    if not text.startswith("---\n") or "\n---" not in text[3:]:
        return None
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if line.split(":", 1)[0].strip() == "model" and ":" in line:
            value = line.split(":", 1)[1].strip().strip("'\"")
            return value if value in AGENT_MODELS else None
    return None


def current_model(payload, tool_input, home=ROOT):
    """Explicit tool_input model, else the named agent's frontmatter model (project, then user)."""
    if tool_input.get("model"):
        return tool_input["model"]
    name = tool_input.get("subagent_type")
    if not isinstance(name, str) or not AGENT_NAME_RE.match(name) or ".." in name:
        return None
    roots = []
    if isinstance(payload.get("cwd"), str) and payload["cwd"]:
        roots.append(Path(payload["cwd"]) / ".claude")
    roots.append(Path(home))
    for root in roots:
        model = frontmatter_model(root / "agents" / f"{name}.md")
        if model:
            return model
    return None




def desc_hash(description):
    """sha256(description)[:12], or None if description is empty/missing."""
    if not description:
        return None
    return hashlib.sha256(description.encode("utf-8")).hexdigest()[:12]


def build_state(tool_input, cfg):
    state = {"subagent_type": tool_input.get("subagent_type") or "general-purpose",
             "description": tool_input.get("description") or ""}
    if cfg.get("send_prompt", True):
        state["prompt"] = str(tool_input.get("prompt") or "")[:int(cfg["max_prompt_chars"])]
    return state


def escalation(current, choice, probabilities, cfg):
    """(tier, mass) when the classifier keeps the current tier but stronger tiers
    together carry at least escalate_mass probability; else None."""
    if current not in TIER_RANK or choice != current:
        return None
    ranks = {t: r for t, r in TIER_RANK.items() if t in cfg["labels"]}
    return stronger_tier(probabilities, ranks, TIER_RANK[current], cfg["escalate_mass"])


def threshold(current, choice, cfg):
    """Upgrades need little confidence, downgrades a lot; unknown direction uses min_confidence."""
    if current in TIER_RANK and choice in TIER_RANK:
        if TIER_RANK[choice] > TIER_RANK[current]:
            return float(cfg["upgrade_min_confidence"])
        if TIER_RANK[choice] < TIER_RANK[current]:
            return float(cfg["downgrade_min_confidence"])
    return float(cfg["min_confidence"])


def build_questions(cfg):
    return {"model": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": cfg["labels"]}}


def decide(payload, cfg, classify_fn, log_fn=None, home=ROOT):
    """Return the PreToolUse hook output dict, or None to leave the call unchanged.

    classify_fn(body, key) returns the parsed Jev response (None means HTTP); it
    runs under a hard deadline, and any exception or timeout is a no-op.
    """
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Agent", "Task"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not cfg["labels"]:
        return None
    if tool_input.get("subagent_type") in (cfg.get("pinned_agents") or []):
        return None
    if cfg.get("respect_explicit_model") and tool_input.get("model"):
        return None
    current = current_model(payload, tool_input, home)
    state = build_state(tool_input, cfg)
    start = time.monotonic()
    answers = ask(cfg, "route", state, build_questions(cfg), classify_fn)
    latency = elapsed_ms(start)
    try:
        answer = answers["model"]
        choice = answer.get("choice")
        confidence = float(answer.get("confidence"))
        probabilities = answer.get("probabilities")
    except Exception:
        return None
    escalated = escalation(current, choice, probabilities, cfg) if choice in cfg["labels"] else None
    applied_model = escalated[0] if escalated else choice
    if choice not in cfg["labels"]:
        applied, why = False, "invalid label"
    elif escalated:
        applied, why = True, "applied"
    elif choice == current:
        applied, why = False, "same model"
    elif confidence < threshold(current, choice, cfg):
        applied, why = False, "below confidence threshold"
    else:
        applied, why = True, "applied"
    if escalated:
        reason = f"jev: {current} → {applied_model} (escalated, P(stronger) {escalated[1]:.2f})"
    else:
        reason = f"jev: {current or 'inherit'} → {choice} (conf {confidence:.2f})"
    if log_fn:
        entry = {"ts": timestamp(), "feature": "route",
                 "subagent_type": state["subagent_type"],
                 "current": current or "inherit", "choice": choice, "confidence": confidence,
                 "probabilities": probabilities, "latency_ms": latency, "applied": applied,
                 "reason": reason if applied else f"{reason}; skipped: {why}"}
        if escalated:
            entry["escalated_to"] = applied_model
        hashed = desc_hash(state["description"])
        if hashed:
            entry["desc_hash"] = hashed
        log_fn(entry)
    if not applied:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": reason,
        "updatedInput": {**tool_input, "model": applied_model},
    }}


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        cfg = load_config()
        output = decide(payload, cfg, None, lambda entry: write_log(cfg, entry))
        if output is not None:
            sys.stdout.write(json.dumps(output))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
