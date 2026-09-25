#!/usr/bin/env python3
"""Jev router — opt-in PreToolUse hook that picks a subagent's model tier.

Reads a Claude Code PreToolUse payload on stdin. For Agent/Task calls it asks
TypeSafe's hosted Jev classifier which tier (sonnet/opus/fable) should run the
task and, when the answer is confident and different, rewrites the tool
input's `model`. Configuration lives under the "jev" key of
`<install>/relay/config.json`; the API key comes from TYPESAFE_API_KEY or
`jev.api_key_file`.

Fail-open: any missing key, disabled config, error, timeout or low-confidence
answer prints nothing and exits 0, leaving the call unchanged. The classifier
call runs under a hard wall-clock deadline of min(timeout_seconds, 4) seconds so
the script always finishes inside the hook's 5 s timeout.
"""
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jev_client import (  # noqa: E402  (re-exported for callers and tests)
    AGENT_MODELS, CONFIG_PATH, DEFAULTS, LOG_PATH, MAX_DEADLINE_SECONDS, MAX_LOG_BYTES, ROOT,
    api_key, ask, call_with_deadline, effective_timeout, elapsed_ms, endpoint_allowed, http_classify,
    load_config, safe_repr, timestamp, write_log)

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
INSTRUCTIONS = ("Which model tier should run this subagent task? "
                "Pick the cheapest tier that can do it well.")


def frontmatter_model(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except Exception:
        return None
    if not text.startswith("---\n") or "\n---" not in text[3:]:
        return None
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if line.split(":", 1)[0].strip() == "model" and ":" in line:
            value = line.split(":", 1)[1].strip().strip("'\"")
            # A full model id of a known tier (claude-opus-4-1) counts as that tier too.
            return value if value in AGENT_MODELS or model_tier(value, AGENT_MODELS) in AGENT_MODELS else None
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


def model_tier(model, tiers):
    """The tier alias for a model: the alias itself, or the tier named inside a full
    Claude id (claude-opus-4-1, us.anthropic.claude-3-5-haiku-...); else the model."""
    if not isinstance(model, str):
        return model
    lowered = model.strip().lower()
    if lowered in tiers:
        return lowered
    if "claude" in lowered:
        for tier in tiers:
            if re.search(rf"(?<![a-z0-9]){re.escape(tier)}(?![a-z0-9])", lowered):
                return tier
    return model


def valid_confidence(confidence):
    return isinstance(confidence, float) and math.isfinite(confidence) and 0.0 <= confidence <= 1.0


def verdict(choice, confidence, current, cfg):
    """Why an answer is skipped, or "applied" (shared with bin/eval-jev-routing.py)."""
    if not isinstance(choice, str) or choice not in cfg["labels"]:
        return "invalid label"
    if not valid_confidence(confidence):
        return "invalid confidence"
    if confidence < float(cfg["min_confidence"]):
        return "below min_confidence"
    if choice == model_tier(current, cfg["labels"]):
        return "same model"
    return "applied"


def pinned(tool_input, cfg):
    """True when the call is never classified (pinned agent or respected explicit model)."""
    if tool_input.get("subagent_type") in (cfg.get("pinned_agents") or []):
        return True
    return bool(cfg.get("respect_explicit_model") and tool_input.get("model"))


def desc_hash(description):
    """sha256(description)[:12], or None if description is empty/missing."""
    if not description:
        return None
    return hashlib.sha256(description.encode("utf-8", errors="replace")).hexdigest()[:12]


def build_state(tool_input, cfg):
    state = {"subagent_type": tool_input.get("subagent_type") or "general-purpose",
             "description": tool_input.get("description") or ""}
    if cfg.get("send_prompt", True):
        state["prompt"] = str(tool_input.get("prompt") or "")[:int(cfg["max_prompt_chars"])]
    return state


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
    if pinned(tool_input, cfg):
        return None
    current = current_model(payload, tool_input, home)
    state = build_state(tool_input, cfg)
    start = time.monotonic()
    errors = []
    answers = ask(cfg, "route", state, build_questions(cfg), classify_fn, errors=errors)
    latency = elapsed_ms(start)
    entry = {"ts": timestamp(), "feature": "route", "subagent_type": state["subagent_type"],
             "current": current or "inherit", "latency_ms": latency}
    hashed = desc_hash(state["description"])
    if hashed:
        entry["desc_hash"] = hashed
    if answers is None:
        # Log only real failures (never the body or key); disabled features stay silent.
        if log_fn and errors:
            log_fn({**entry, "applied": False, "reason": "unavailable", "error": errors[0]})
        return None
    try:
        answer = answers["model"]
        choice = answer.get("choice")
        probabilities = answer.get("probabilities")
    except Exception:
        # Malformed shape (e.g. answers["model"] is a string), same as ask()'s own check.
        if log_fn:
            log_fn({**entry, "applied": False, "reason": "unavailable", "error": "MalformedResponse"})
        return None
    raw_confidence = answer.get("confidence")
    if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
        confidence = float(raw_confidence)
    else:
        confidence = None
    why = verdict(choice, confidence, current, cfg)
    applied = why == "applied"
    conf_text = f"{confidence:.2f}" if valid_confidence(confidence) else "invalid"
    if confidence is not None and not math.isfinite(confidence):
        confidence = None  # keep the log strict JSON
    # Never write a raw non-string/oversized choice; repr() is ASCII-safe (handles lone surrogates too).
    display_choice = safe_repr(choice) if why == "invalid label" else choice
    reason = f"jev: {current or 'inherit'} → {display_choice} (conf {conf_text})"
    if log_fn:
        entry.update({"choice": display_choice, "confidence": confidence, "probabilities": probabilities,
                      "applied": applied, "reason": reason if applied else f"{reason}; skipped: {why}"})
        log_fn(entry)
    if not applied:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": reason,
        "updatedInput": {**tool_input, "model": choice},
    }}


def main():
    try:
        # Hook payloads are UTF-8 whatever the locale (e.g. cp1255 on Windows).
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        cfg = load_config()
        output = decide(payload, cfg, None, lambda entry: write_log(cfg, entry))
        if output is not None:
            sys.stdout.write(json.dumps(output))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
