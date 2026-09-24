"""Shared stdlib client for the opt-in Jev integrations.

Every Jev feature (model routing, task-shift detection, risk gate, subagent
report check, handoff grading) loads its settings from the "jev" key of
`<install>/relay/config.json` and asks TypeSafe's hosted classifier through
`ask()`. The API key comes from TYPESAFE_API_KEY or `jev.api_key_file`.

Fail-open contract: `ask()` returns None when the feature is disabled, the key
is missing, the endpoint is not allowed, or the call errors or outlives its
hard deadline of min(timeout_seconds, 4) seconds. Callers treat None as "no
opinion" and keep their pre-Jev behaviour. Nothing here logs prompt, diff or
report text, or the key.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "relay" / "config.json"
LOG_PATH = ROOT / "relay" / "jev-log.jsonl"
AGENT_MODELS = ("sonnet", "opus", "haiku", "fable")
FEATURES = ("route", "shift", "risk_gate", "report_check", "handoff_grade")
MAX_DEADLINE_SECONDS = 4.0
MAX_LOG_BYTES = 1 << 20
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULTS = {
    # Off unless install.py --jev (or the user) sets relay/config.json jev.enabled.
    "enabled": False,
    "endpoint": "https://api.typesafe.ai/v1/systemone",
    "jev_model": "jev-latest",
    "timeout_seconds": 3,
    "log": True,
    "features": {name: True for name in FEATURES},
    # route
    "min_confidence": 0.5,
    "respect_explicit_model": False,
    "pinned_agents": ["critic", "fork"],
    "max_prompt_chars": 6000,
    "send_prompt": True,
    "labels": {
        "sonnet": ("Bounded, fully specified work with no open decisions: locate/read/summarize "
                   "code, run a given command and report output, apply an edit whose exact change "
                   "is already spelled out, mechanical renames or formatting."),
        "opus": ("Implementation that requires the agent to make its own decisions: changes "
                 "spanning several files, refactors or features where the details are not spelled "
                 "out, adding validation or retry logic, writing tests that need design, fixing a "
                 "flaky or failing test in a known area, keeping two implementations consistent."),
        "fable": ("Hard reasoning: architecture/design decisions, root-causing unclear bugs, "
                  "security or concurrency logic, adversarial review of risky changes."),
    },
    # shift
    "shift_low": 0.25,
    "shift_high": 0.75,
    # risk_gate
    "send_diff": True,
    "max_diff_chars": 12000,
    "risk_min_probability": 0.6,
    # report_check
    "report_roles": ["scout", "runner", "builder", "critic"],
    "report_min_support": 0.5,
    "report_max_gap": 0.5,
    "max_report_chars": 8000,
    # handoff_grade
    "handoff_min_score": 2,
}


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    user = {}
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8")).get("jev")
        if isinstance(loaded, dict):
            user = loaded
    except Exception:
        pass
    cfg.update({k: v for k, v in user.items() if k not in ("labels", "features")})
    # User labels merge over the defaults; a null value removes that tier.
    labels = dict(DEFAULTS["labels"])
    if isinstance(user.get("labels"), dict):
        for name, rubric in user["labels"].items():
            if rubric is None:
                labels.pop(name, None)
            elif isinstance(rubric, str):
                labels[name] = rubric
    cfg["labels"] = {k: v for k, v in labels.items() if k in AGENT_MODELS}
    features = dict(DEFAULTS["features"])
    if isinstance(user.get("features"), dict):
        features.update({k: bool(v) for k, v in user["features"].items() if k in FEATURES})
    cfg["features"] = features
    if not isinstance(cfg.get("pinned_agents"), list):
        cfg["pinned_agents"] = []
    return cfg


def feature_enabled(cfg, name):
    return bool(cfg.get("enabled", False)) and bool(cfg.get("features", {}).get(name, False))


def endpoint_allowed(endpoint):
    """Only https, or plain http to a loopback host."""
    try:
        parts = urllib.parse.urlsplit(str(endpoint))
    except ValueError:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS


def effective_timeout(cfg):
    try:
        timeout = float(cfg.get("timeout_seconds"))
    except (TypeError, ValueError):
        timeout = MAX_DEADLINE_SECONDS
    return max(0.0, min(timeout, MAX_DEADLINE_SECONDS))


def call_with_deadline(fn, args, timeout):
    """Run fn(*args) in a daemon thread; raise TimeoutError if it outlives timeout."""
    box = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("classifier deadline exceeded")
    if "error" in box:
        raise box["error"]
    return box["value"]


def api_key(cfg):
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    path = cfg.get("api_key_file")
    if isinstance(path, str) and path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
        except Exception:
            return None
    if cfg.get("api_key_source") == "claude_settings":
        try:
            settings = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
            value = settings.get("env", {}).get("TYPESAFE_API_KEY")
            return value.strip() or None if isinstance(value, str) else None
        except Exception:
            return None
    return None


def http_classify(body, cfg, key):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    request = urllib.request.Request(
        cfg["endpoint"], data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=max(effective_timeout(cfg), 0.1)) as response:
        return json.loads(response.read().decode("utf-8"))


def ask(cfg, feature, state, questions, classify_fn=None):
    """Return the Jev `answers` dict, or None (disabled, no key, error, timeout).

    classify_fn(body, key) returns the parsed response; it defaults to the HTTP
    call and always runs under the hard deadline.
    """
    if not feature_enabled(cfg, feature) or not endpoint_allowed(cfg.get("endpoint")):
        return None
    key = api_key(cfg)
    if not key:
        return None
    fn = classify_fn or (lambda body, k: http_classify(body, cfg, k))
    body = {"state": state, "model": cfg["jev_model"], "questions": questions}
    try:
        answers = call_with_deadline(fn, (body, key), effective_timeout(cfg))["answers"]
    except Exception:
        return None
    return answers if isinstance(answers, dict) else None


def noul(answers, name):
    """The yes-probability of a noul answer, or None if absent/malformed."""
    try:
        value = float(answers[name]["noul"])
    except Exception:
        return None
    return value if 0.0 <= value <= 1.0 else None


def elapsed_ms(start):
    return int((time.monotonic() - start) * 1000)


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_log(cfg, entry, path=None):
    if not cfg.get("log", True):
        return
    path = Path(path or LOG_PATH)
    try:
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
