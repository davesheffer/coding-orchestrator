#!/usr/bin/env python3
"""Opt-in Codex lifecycle adapter for TypeSafe Jev. Fail open on API errors."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import jev_client as client  # noqa: E402

CONFIG = HOME / "jev" / "config.json"
STATE = HOME / "jev" / "state"
LOG = HOME / "jev" / "jev-log.jsonl"
MODELS = {"luna": "gpt-6-luna", "sol": "gpt-6-sol", "astra": "gpt-6-astra"}
PINNED = {"scout", "runner", "builder", "critic"}
LABELS = {
    "luna": "Bounded searches, summaries and exact offline checks.",
    "sol": "Implementation and routine design or debugging.",
    "astra": "Security, concurrency, migrations, data loss, public APIs or hard review.",
}


def guard_module():
    path = Path(__file__).with_name("jev-guard.py")
    if not path.is_file():
        path = Path(__file__).resolve().parent.parent / "bin" / "jev-guard.py"
    spec = importlib.util.spec_from_file_location("codex_jev_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings():
    cfg = client.load_config(CONFIG)
    # load_config keeps only Claude tiers, so merge the user's Codex labels from the
    # raw file the same way: over the defaults, with a null value removing a tier.
    labels = dict(LABELS)
    try:
        user = json.loads(CONFIG.read_text(encoding="utf-8")).get("jev", {}).get("labels")
    except Exception:
        user = None
    if isinstance(user, dict):
        for name, rubric in user.items():
            if rubric is None:
                labels.pop(name, None)
            elif isinstance(rubric, str):
                labels[name] = rubric
    cfg["labels"] = {k: v for k, v in labels.items() if k in MODELS}
    return cfg


def log(cfg, entry):
    client.write_log(cfg, entry, LOG)


def desc_hash(description):
    """sha256(description)[:12] as in bin/jev-route.py, or None if empty."""
    if not description:
        return None
    return hashlib.sha256(description.encode("utf-8", errors="replace")).hexdigest()[:12]


def coerce_confidence(raw):
    """The confidence as a float in [0, 1], or None if raw isn't a usable number.

    Same rule as bin/jev-route.py: rejects bool and str, and treats a huge int
    (float() raises OverflowError) as invalid instead of letting route() raise.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except (OverflowError, ValueError, TypeError):
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def route(payload, cfg, classify_fn=None):
    if payload.get("tool_name") not in ("Agent", "Task", "spawn_agent"):
        return None
    args = payload.get("tool_input")
    if not isinstance(args, dict) or not cfg["labels"]:
        return None
    role = args.get("agent_type") or args.get("subagent_type")
    if role in PINNED or role in cfg.get("pinned_agents", []):
        return None
    if role not in (None, "default", "general-purpose") or args.get("model"):
        return None
    state = {"role": role or "default"}
    task = str(args.get("message") or args.get("prompt") or "")
    if cfg.get("send_prompt", True):
        state["task"] = task[:cfg["max_prompt_chars"]]
    start = time.monotonic()
    errors = []
    answers = client.ask(cfg, "route", state,
                         {"model": {"type": "choice", "instructions": "Choose the cheapest Codex model that can complete this task well.",
                                    "criteria": cfg["labels"]}}, classify_fn, errors=errors)
    # Same shape as bin/jev-route.py entries so jev-report buckets them by agent.
    entry = {"ts": client.timestamp(), "feature": "route", "subagent_type": role or "default",
             "role": role or "default", "latency_ms": client.elapsed_ms(start)}
    hashed = desc_hash(str(args.get("description") or task))
    if hashed:
        entry["desc_hash"] = hashed
    if answers is None:
        if errors:
            log(cfg, {**entry, "applied": False, "reason": "unavailable", "error": errors[0]})
        return None
    try:
        answer = answers["model"]
        choice = answer["choice"]
    except (TypeError, KeyError):
        # Malformed shape (e.g. answers["model"] is a string), same as client.ask()'s own check.
        log(cfg, {**entry, "applied": False, "reason": "unavailable", "error": "MalformedResponse"})
        return None
    confidence = coerce_confidence(answer.get("confidence"))
    if not isinstance(choice, str) or choice not in MODELS or choice not in cfg["labels"]:
        why = "invalid label"
    elif confidence is None:
        why = "invalid confidence"
    elif confidence < float(cfg["min_confidence"]):
        why = "below min_confidence"
    else:
        why = "applied"
    # Never write a raw non-string/oversized choice; repr() is ASCII-safe (handles lone surrogates too).
    display_choice = client.safe_repr(choice) if why == "invalid label" else choice
    log(cfg, {**entry, "choice": display_choice, "confidence": confidence, "applied": why == "applied",
              "reason": why})
    if why != "applied":
        return None
    updated = {**args, "model": MODELS[choice]}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                    "updatedInput": updated}}


def risk(payload, cfg, classify_fn=None):
    guard = guard_module()
    result = guard.gate(payload, cfg, classify_fn, lambda entry: log(cfg, entry), state_dir=STATE)
    if result:
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        result["hookSpecificOutput"]["permissionDecisionReason"] = reason.replace("CLAUDE.md", "AGENTS.md")
    return result


def subagent_start(payload, cfg):
    if payload.get("agent_type") != "critic" or not client.feature_enabled(cfg, "risk_gate"):
        return None
    guard = guard_module()
    sid = payload.get("session_id")
    aid = payload.get("agent_id")
    if sid and aid:
        guard.update_state(guard.session_state_path(sid, STATE),
                           lambda s: s.setdefault("critic_started", {}).update({aid: time.time()}))
    return None


def subagent_stop(payload, cfg, classify_fn=None):
    role = payload.get("agent_type")
    if role not in PINNED:
        return None
    message = payload.get("last_assistant_message")
    if not isinstance(message, str):
        return None
    guard = guard_module()
    if role == "critic" and client.feature_enabled(cfg, "risk_gate"):
        sid, aid = payload.get("session_id"), payload.get("agent_id")
        if sid and aid:
            def complete(s):
                started = s.get("critic_started") or {}
                when = started.pop(aid, None)
                if isinstance(when, (int, float)):
                    s["critic_ts"] = max(s.get("critic_ts") or 0, when)
            guard.update_state(guard.session_state_path(sid, STATE), complete)
    if not client.feature_enabled(cfg, "report_check") or role not in cfg.get("report_roles", []):
        return None
    reasons, codes, supported, gap = guard._analyze_report(message, cfg, classify_fn, confidence_heuristic=False)
    log(cfg, {"ts": client.timestamp(), "feature": "report_check", "role": role,
              "weak": bool(reasons), "reasons": codes, "supported": supported, "material_gap": gap})
    if reasons and not payload.get("stop_hook_active"):
        return {"decision": "block", "reason": "[jev report check] " + "; ".join(reasons) +
                ". Verify the claim and include RESULT, EVIDENCE, CONFIDENCE, UNVERIFIED; or state the gap clearly."}
    return None


def shift(payload, cfg, classify_fn=None):
    if not client.feature_enabled(cfg, "shift"):
        return None
    prompt = payload.get("prompt")
    sid = payload.get("session_id")
    if not isinstance(prompt, str) or not isinstance(sid, str):
        return None
    guard = guard_module()
    path = guard.session_state_path(sid, STATE)
    state = guard.load_session_state(sid, STATE)
    recent = state.get("recent_prompts", [])
    context = None
    if recent:
        answers = client.ask(cfg, "shift", {"recent_prompts": recent[-3:], "new_prompt": prompt[:2000]},
                             {"continues": {"type": "noul", "instructions":
                                            "Does this prompt continue the recent task, including corrections and follow-ups?"}}, classify_fn)
        p = client.noul(answers, "continues")
        if p is not None and p < cfg["shift_low"]:
            context = "[jev] This appears to start a new task. Preserve the current task state and follow the user's latest instruction."
        log(cfg, {"ts": client.timestamp(), "feature": "shift", "p": p,
                  "decision": "new" if context else "continue_or_unsure"})
    guard.update_state(path, lambda s: s.update(recent_prompts=(recent + [prompt[:2000]])[-3:]))
    if context:
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    return None


def grade_handoff(body, cfg, classify_fn=None):
    answers = client.ask(cfg, "handoff_grade", {"handoff": body[:12000]},
                         {"actionable": {"type": "score", "instructions":
                                         "How actionable is this handoff for a fresh Codex session?",
                                         "criteria": ["unusable", "vague", "workable", "solid", "excellent"]},
                          "next_step_concrete": {"type": "noul", "instructions":
                                                 "Does NEXT STEP name a concrete immediate action?"},
                          "verified_backed": {"type": "noul", "instructions":
                                              "Are VERIFIED claims backed by commands and results?"}}, classify_fn)
    try:
        score = float(answers["actionable"]["score"])
    except (TypeError, KeyError, ValueError):
        return None
    log(cfg, {"ts": client.timestamp(), "feature": "handoff_grade", "score": score})
    return {"score": score, "weak": score < cfg["handoff_min_score"]}


def main():
    try:
        # Hook payloads are UTF-8 whatever the locale (e.g. cp1255 on Windows).
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        cfg = settings()
        if len(sys.argv) > 1 and sys.argv[1] == "grade":
            result = grade_handoff(str(payload.get("handoff") or ""), cfg)
            if result is not None:
                print(json.dumps(result))
            return 0
        event = payload.get("hook_event_name")
        result = None
        if event == "PreToolUse":
            result = risk(payload, cfg) if payload.get("tool_name") == "Bash" else route(payload, cfg)
        elif event == "SubagentStart":
            result = subagent_start(payload, cfg)
        elif event == "SubagentStop":
            result = subagent_stop(payload, cfg)
        elif event == "UserPromptSubmit":
            result = shift(payload, cfg)
        if result is not None:
            print(json.dumps(result))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
