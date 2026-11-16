#!/usr/bin/env python3
"""Jev router — opt-in PreToolUse hook that picks a subagent's model tier.

Reads a Claude Code PreToolUse payload on stdin. For Agent/Task calls it asks
TypeSafe's hosted Jev classifier which tier (sonnet/opus/fable) should run the
task and, when the answer is confident enough and different, rewrites the tool
input's `model`. Moving to a stronger tier needs less confidence than moving to
a weaker one, and a task escalates when the stronger tiers together are likely
enough, because under-routing costs more than over-routing. A call that sets `model`
itself is never moved to a weaker tier when the hook can rank
that model. Configuration lives under the "jev" key of
`<install>/relay/config.json`; the API key comes from TYPESAFE_API_KEY or
`jev.api_key_file`.

Fail-open: any missing key, disabled config, error, timeout or low-confidence
answer prints nothing and exits 0, leaving the call unchanged. The one exception,
while routing is on: a built-in agent that would inherit the main session's model
runs on `inherit_fallback` (default sonnet) when Jev gives no usable answer. The classifier
call runs under a hard wall-clock deadline of min(timeout_seconds, 4) seconds so
the script always finishes inside the hook's 5 s timeout.
"""
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jev_client import (  # noqa: E402  (re-exported for callers and tests)
    AGENT_MODELS, CONFIG_PATH, DEFAULTS, LOG_PATH, MAX_DEADLINE_SECONDS, MAX_LOG_BYTES, ROOT, TIER_RANK,
    coerce_confidence, stronger_tier,
    api_key, ask, call_with_deadline, effective_timeout, elapsed_ms, endpoint_allowed, feature_enabled,
    http_classify, load_config, safe_repr, session_log, timestamp, write_log)

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# Built-in agents that run on the main session's model when a call names no model.
INHERITING_AGENTS = ("general-purpose", "Explore", "Plan")
# Claude Code model aliases that run a known tier (opusplan plans with opus).
MODEL_ALIASES = {"opusplan": "opus"}
INSTRUCTIONS = ("Which model tier should run this subagent task? "
                "Pick the least expensive tier that will reliably do it well; "
                "when torn between two tiers, pick the stronger one.")


def frontmatter_value(path):
    """The raw `model:` value of an agent file's frontmatter, or None."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except Exception:
        return None
    if not text.startswith("---\n") or "\n---" not in text[3:]:
        return None
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if line.split(":", 1)[0].strip() == "model" and ":" in line:
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def frontmatter_model(path):
    value = frontmatter_value(path)
    if value is None:
        return None
    # A full model id of a known tier (claude-opus-4-1) counts as that tier too.
    return value if value in AGENT_MODELS or model_tier(value, AGENT_MODELS) in AGENT_MODELS else None


def agent_files(payload, name, home=ROOT):
    """Candidate agent files for subagent_type `name`: project, then user."""
    if not isinstance(name, str) or not AGENT_NAME_RE.match(name) or ".." in name:
        return []
    roots = []
    if isinstance(payload.get("cwd"), str) and payload["cwd"]:
        roots.append(Path(payload["cwd"]) / ".claude")
    roots.append(Path(home))
    return [root / "agents" / f"{name}.md" for root in roots]


def current_model(payload, tool_input, home=ROOT):
    """Explicit tool_input model, else the named agent's frontmatter model (project, then user)."""
    model = tool_input.get("model")
    if isinstance(model, str) and model.strip():
        return model  # an empty/blank string counts as unset
    for path in agent_files(payload, tool_input.get("subagent_type"), home):
        model = frontmatter_model(path)
        if model:
            return model
    return None


def explicit_model(tool_input):
    """True when the call sets `model` itself: a non-blank string, as in current_model."""
    model = tool_input.get("model")
    return isinstance(model, str) and bool(model.strip())


def model_tier(model, tiers):
    """The tier alias for a model: the alias itself (or its MODEL_ALIASES tier), or the tier
    from `tiers` named inside a full Claude id (claude-opus-4-1 is opus when "opus" is in
    tiers); else the model."""
    if not isinstance(model, str):
        return model
    lowered = model.strip().lower()
    lowered = MODEL_ALIASES.get(lowered, lowered)
    if lowered in tiers:
        return lowered
    if "claude" in lowered:
        for tier in tiers:
            if re.search(rf"(?<![a-z0-9]){re.escape(tier)}(?![a-z0-9])", lowered):
                return tier
    return model


def invalid_model(model):
    """True for a tool_input model that is set but not a string (list, dict, number, bool)."""
    return model is not None and not isinstance(model, str)


def bounded(value):
    """value for the log, reason text or classifier state: a short string as is, else safe_repr."""
    return value if isinstance(value, str) and len(value) <= 80 else safe_repr(value)


def display_model(model):
    """model for the log and reason text, "inherit" when unknown."""
    return "inherit" if model is None else bounded(model)


def valid_confidence(confidence):
    return isinstance(confidence, float) and math.isfinite(confidence) and 0.0 <= confidence <= 1.0


def sanitize_probabilities(probabilities, labels):
    """The probabilities dict for logging: dict only, configured labels only (keys are
    classifier-controlled), values that aren't a probability become None."""
    if not isinstance(probabilities, dict):
        return None
    return {k: coerce_confidence(v) for k, v in probabilities.items() if k in labels}


def verdict(choice, confidence, current, cfg, probabilities=None, explicit=False):
    """(why, model, escalated_mass): why an answer is skipped, or "applied" with the
    model to run (shared with bin/eval-jev-routing.py). escalated_mass is set when
    the classifier kept the current tier but stronger tiers together are likely enough.
    explicit: the call set `current` itself, so it is never moved to a weaker tier."""
    if not isinstance(current, str):
        current = None  # e.g. a dict/list tool_input.model: treat as unknown
    if not isinstance(choice, str) or choice not in cfg["labels"]:
        return "invalid label", None, None
    if not valid_confidence(confidence):
        return "invalid confidence", None, None
    # Resolve against every known tier, so thresholds don't depend on which labels are kept.
    tier = model_tier(current, TIER_RANK)
    escalated = escalation(tier, choice, probabilities, cfg)
    if escalated:
        return "applied", escalated[0], escalated[1]
    if choice == tier:
        return "same model", None, None
    if explicit and tier in TIER_RANK and choice in TIER_RANK and TIER_RANK[choice] < TIER_RANK[tier]:
        # An explicit model the hook cannot rank keeps the threshold rule below.
        return "explicit model kept", None, None
    if confidence < threshold(tier, choice, cfg):
        return "below confidence threshold", None, None
    return "applied", choice, None


def pinned(tool_input, cfg):
    """True when the call is never classified (pinned agent or respected explicit model)."""
    if tool_input.get("subagent_type") in (cfg.get("pinned_agents") or []):
        return True
    # Same predicate as current_model: only a non-blank string pins; a non-string is invalid
    # (logged by decide), not a silent pin.
    return bool(cfg.get("respect_explicit_model") and explicit_model(tool_input))


def desc_hash(description):
    """sha256(description)[:12], or None if description is empty/missing."""
    if not description:
        return None
    return hashlib.sha256(description.encode("utf-8", errors="replace")).hexdigest()[:12]


def build_state(tool_input, cfg):
    description = tool_input.get("description")  # a non-string counts as absent
    state = {"subagent_type": bounded(tool_input.get("subagent_type") or "general-purpose"),
             "description": description if isinstance(description, str) else ""}
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


def fallback_model(payload, tool_input, current, cfg, home=ROOT):
    """jev.inherit_fallback for a built-in agent that would inherit the main model, else None.

    Only while routing is on (jev.enabled and features.route), so the kill switches still turn
    the hook off. A call is inheriting only when it names no model, no agent file pins one, and
    no CLAUDE_CODE_SUBAGENT_MODEL default applies. An agent file with any model but
    `inherit` (even one this hook cannot rank, such as opus[1m]) pins that model.
    """
    model = cfg.get("inherit_fallback")
    if current is not None or not isinstance(model, str) or model not in AGENT_MODELS:
        return None
    if not feature_enabled(cfg, "route"):
        return None
    default = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL", "").strip().lower()
    if default and default != "inherit":
        return None
    name = tool_input.get("subagent_type")
    if not (name is None or name == "" or name in INHERITING_AGENTS):
        return None
    for path in agent_files(payload, name, home):
        value = frontmatter_value(path)
        if value and value.lower() != "inherit":
            return None
    return model


def falls_back(why, fallback):
    """True when Jev answered but the answer is unusable (a usable answer always wins)."""
    return bool(fallback) and why in ("invalid label", "invalid confidence")


def fallback_output(tool_input, model, why):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": f"jev: inherit → {model} (fallback: {why})",
        "updatedInput": {**tool_input, "model": model},
    }}


def build_questions(cfg):
    return {"model": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": cfg["labels"]}}


def decide(payload, cfg, classify_fn, log_fn=None, home=ROOT):
    """Return the PreToolUse hook output dict, or None to leave the call unchanged.

    classify_fn(body, key) returns the parsed Jev response (None means HTTP); it
    runs under a hard deadline. Any exception or timeout is a no-op, except that an
    inheriting built-in call then runs on the jev.inherit_fallback tier.
    """
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Agent", "Task"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not cfg["labels"]:
        return None
    if pinned(tool_input, cfg):
        return None
    state = build_state(tool_input, cfg)
    if invalid_model(tool_input.get("model")):
        # A malformed explicit model is never rewritten (nor classified); log it bounded.
        if log_fn and feature_enabled(cfg, "route"):
            log_fn({"ts": timestamp(), "feature": "route", "subagent_type": state["subagent_type"],
                    "current": safe_repr(tool_input["model"]), "applied": False,
                    "reason": "skipped: invalid model"})
        return None
    current = current_model(payload, tool_input, home)
    fallback = fallback_model(payload, tool_input, current, cfg, home)
    shown = display_model(current)
    start = time.monotonic()
    errors = []
    answers = ask(cfg, "route", state, build_questions(cfg), classify_fn, errors=errors)
    latency = elapsed_ms(start)
    entry = {"ts": timestamp(), "feature": "route", "subagent_type": state["subagent_type"],
             "current": shown, "latency_ms": latency}
    hashed = desc_hash(state["description"])
    if hashed:
        entry["desc_hash"] = hashed
    if answers is None:
        # Log only real failures (never the body or key); disabled features stay silent.
        # "applied" counts Jev's own routes only; a fallback is logged under "fallback".
        if fallback:
            if log_fn:
                log_fn({**entry, "applied": False, "fallback": fallback, "reason": "unavailable",
                        **({"error": errors[0]} if errors else {})})
            return fallback_output(tool_input, fallback, "unavailable")
        if log_fn and errors:
            log_fn({**entry, "applied": False, "reason": "unavailable", "error": errors[0]})
        return None
    try:
        answer = answers["model"]
        choice = answer["choice"]
        probabilities = answer.get("probabilities")
    except Exception:
        choice = None
    if not isinstance(choice, str):
        # Malformed shape (answers["model"] not a dict, no string choice), same as ask()'s own
        # check and codex/jev-hook.py; an unknown string label is "invalid label" below.
        if log_fn:
            log_fn({**entry, "applied": False, "reason": "unavailable", "error": "MalformedResponse",
                    **({"fallback": fallback} if fallback else {})})
        return fallback_output(tool_input, fallback, "unavailable") if fallback else None
    confidence = coerce_confidence(answer.get("confidence"))
    why, applied_model, escalated_mass = verdict(choice, confidence, current, cfg, probabilities,
                                                 explicit_model(tool_input))
    applied = why == "applied"
    conf_text = f"{confidence:.2f}" if valid_confidence(confidence) else "invalid"
    # Never write a raw non-string/oversized choice; repr() is UTF-8-safe (handles lone surrogates too).
    display_choice = safe_repr(choice) if why == "invalid label" else choice
    if escalated_mass is not None:
        reason = f"jev: {shown} → {applied_model} (escalated, P(stronger) {escalated_mass:.2f})"
    else:
        reason = f"jev: {shown} → {display_choice} (conf {conf_text})"
    if log_fn:
        entry.update({"choice": display_choice, "confidence": confidence,
                      "probabilities": sanitize_probabilities(probabilities, cfg["labels"]),
                      "applied": applied, "reason": reason if applied else f"{reason}; skipped: {why}"})
        if escalated_mass is not None:
            entry["escalated_to"] = applied_model
        if not applied and falls_back(why, fallback):
            entry["fallback"] = fallback
        log_fn(entry)
    if not applied:
        # A usable answer below threshold keeps inheriting: under-routing costs more.
        return fallback_output(tool_input, fallback, why) if falls_back(why, fallback) else None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": reason,
        "updatedInput": {**tool_input, "model": applied_model},
    }}


def main():
    try:
        # Hook payloads are UTF-8 whatever the locale (e.g. cp1255 on Windows).
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        cfg = load_config()
        log_fn = session_log(cfg, payload.get("session_id") if isinstance(payload, dict) else None)
        output = decide(payload, cfg, None, log_fn)
        if output is not None:
            sys.stdout.write(json.dumps(output))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
