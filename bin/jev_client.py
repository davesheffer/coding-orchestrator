"""Shared stdlib client for the opt-in Jev integrations.

Every Jev feature (model routing, task-shift detection, risk gate, subagent
report check, handoff grading, the ask-jev CLI) loads its settings from the "jev" key of
`<install>/relay/config.json` and asks TypeSafe's hosted classifier through
`ask()`. The API key comes from TYPESAFE_API_KEY or `jev.api_key_file`.

Fail-open contract: `ask()` returns None when the feature is disabled, the key
is missing, the endpoint is not allowed, or the call errors or outlives its
hard deadline of min(timeout_seconds, 4) seconds. Callers treat None as "no
opinion" and keep their pre-Jev behaviour. Nothing here logs prompt, diff or
report text, or the key. A successful call that reports token usage appends one
`"kind": "usage"` line (tokens and USD, reported or estimated) to the same log.
"""
import csv
import ctypes
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "relay" / "config.json"
LOG_PATH = ROOT / "relay" / "jev-log.jsonl"
AGENT_MODELS = ("sonnet", "opus", "haiku", "fable")
TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
ROUTE_NUMBERS = ("min_confidence", "upgrade_min_confidence", "downgrade_min_confidence", "escalate_mass")
# Every probability/confidence threshold in DEFAULTS: each must be a number in [0, 1].
THRESHOLDS = ROUTE_NUMBERS + ("shift_low", "shift_high", "risk_min_probability",
                              "report_min_support", "report_max_gap")
FEATURES = ("route", "shift", "risk_gate", "report_check", "handoff_grade", "ask")
MAX_DEADLINE_SECONDS = 4.0
# Hooks block the agent, so they keep MAX_DEADLINE_SECONDS; the ask-jev CLI may wait longer.
MAX_ASK_DEADLINE_SECONDS = 60.0
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
    # Under-routing is the costly error, so moving to a stronger tier is easy and
    # moving to a weaker one is hard. min_confidence still covers moves whose
    # direction is unknown (no current model).
    "upgrade_min_confidence": 0.35,
    "downgrade_min_confidence": 0.8,
    # Escalate when Jev keeps the current tier but the stronger tiers together are
    # this likely (Codex, which has no current tier: stronger than Jev's choice).
    "escalate_mass": 0.4,
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
    # ask (bin/ask-jev.py): per-call deadline, capped at MAX_ASK_DEADLINE_SECONDS
    "ask_timeout_seconds": 20,
    # spend ledger: list prices used when a response reports tokens but no cost
    "input_usd_per_million": 0.042,
    "output_usd_per_million": 0.0,
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
    # log_path is internal (codex/jev-hook.py sets it after loading), never user-supplied.
    cfg.update({k: v for k, v in user.items() if k not in ("labels", "features", "log_path")})
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
    # A bad number falls back to its default, so it can't make every hook raise (and fail open
    # silently) later.
    for key in THRESHOLDS:
        value = config_number(cfg[key], 0.0, 1.0)
        cfg[key] = float(DEFAULTS[key]) if value is None else value
    score = config_number(cfg["handoff_min_score"], 0.0, 4.0)
    cfg["handoff_min_score"] = (DEFAULTS["handoff_min_score"] if score is None
                                else int(score) if score.is_integer() else score)
    for key in ("input_usd_per_million", "output_usd_per_million"):
        price = config_number(cfg[key], 0.0, math.inf)
        cfg[key] = float(DEFAULTS[key]) if price is None else price
    chars = config_number(cfg["max_prompt_chars"], 0.0, math.inf)
    cfg["max_prompt_chars"] = (int(chars) if chars is not None and chars.is_integer()
                               else DEFAULTS["max_prompt_chars"])
    ask_timeout = config_number(cfg["ask_timeout_seconds"], 0.1, MAX_ASK_DEADLINE_SECONDS)
    cfg["ask_timeout_seconds"] = DEFAULTS["ask_timeout_seconds"] if ask_timeout is None else ask_timeout
    return cfg


def config_number(value, low, high):
    """value as a finite float in [low, high] (numeric strings too), else None.

    Rejects bool, NaN/inf and a huge int (float() raises OverflowError).
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def probability(value):
    """value as a float in [0, 1], else None.

    Rejects bool and str. float() raises OverflowError on a huge int (a JSON
    number with hundreds of digits), which counts as invalid instead of raising.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        p = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return p if math.isfinite(p) and 0.0 <= p <= 1.0 else None


coerce_confidence = probability  # a classifier confidence follows the same rule


def stronger_tier(probabilities, ranks, floor, mass_needed):
    """(tier, mass) for the likeliest tier ranked above floor when the tiers above
    floor together reach mass_needed; ties go to the stronger tier. Else None."""
    if not isinstance(probabilities, dict):
        return None
    stronger = {}
    for tier, value in probabilities.items():
        p = probability(value)
        if tier in ranks and ranks[tier] > floor and p is not None:
            stronger[tier] = p
    mass = sum(stronger.values())
    if not stronger or mass < mass_needed:
        return None
    return max(stronger, key=lambda t: (stronger[t], ranks[t])), mass


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


def effective_timeout(cfg, cap=MAX_DEADLINE_SECONDS):
    try:
        timeout = float(cfg.get("timeout_seconds"))
    except (TypeError, ValueError):
        timeout = cap
    if not math.isfinite(timeout):
        timeout = cap
    return max(0.0, min(timeout, cap))


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
    # ask() passes a config whose timeout_seconds is already capped for its caller.
    timeout = effective_timeout(cfg, MAX_ASK_DEADLINE_SECONDS)
    with opener.open(request, timeout=max(timeout, 0.1)) as response:
        return json.loads(response.read().decode("utf-8"))


def ask(cfg, feature, state, questions, classify_fn=None, errors=None, deadline=None,
        usage=None, timeout_cap=MAX_DEADLINE_SECONDS):
    """Return the Jev `answers` dict, or None (disabled, no key, error, timeout).

    classify_fn(body, key) returns the parsed response; it defaults to the HTTP
    call and always runs under the hard deadline. When `errors` is a list, a
    failed call appends a short reason (the exception class name, "NoApiKey" or
    "MalformedResponse") so callers can log it; never the body or the key.
    Disabled features and disallowed endpoints append nothing.

    `deadline` (a time.monotonic() value) is the caller's overall budget: the call
    gets min(effective_timeout(cfg), what remains), and none at all once it has
    passed (records "TimeoutError" without calling the classifier).

    A response with valid token usage is logged as a `"kind": "usage"` entry, and
    copied into `usage` when that is a dict. `timeout_cap` raises the per-call
    ceiling for callers that are not hooks (at most MAX_ASK_DEADLINE_SECONDS).
    """
    if not feature_enabled(cfg, feature) or not endpoint_allowed(cfg.get("endpoint")):
        return None
    key = api_key(cfg)
    if not key:
        if errors is not None:
            errors.append("NoApiKey")
        return None
    timeout = effective_timeout(cfg, min(max(float(timeout_cap), 0.0), MAX_ASK_DEADLINE_SECONDS))
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if errors is not None:
                errors.append("TimeoutError")
            return None
        timeout = min(timeout, remaining)
    # http_classify reads its socket timeout from the config.
    run_cfg = dict(cfg, timeout_seconds=timeout)
    fn = classify_fn or (lambda body, k: http_classify(body, run_cfg, k))
    body = {"state": state, "model": cfg["jev_model"], "questions": questions}
    try:
        response = call_with_deadline(fn, (body, key), timeout)
        answers = response["answers"]
    except Exception as exc:
        if errors is not None:
            errors.append(type(exc).__name__)
        return None
    if not isinstance(answers, dict):
        if errors is not None:
            errors.append("MalformedResponse")
        return None
    # Usage accounting is best effort: a malformed usage block never costs the answers.
    try:
        spent = usage_of(cfg, response)
        if spent is not None:
            if isinstance(usage, dict):
                usage.update(spent)
            write_log(cfg, {"ts": timestamp(), "kind": "usage", "feature": feature,
                            "questions": len(questions) if isinstance(questions, dict) else None,
                            **spent})
    except Exception:
        pass
    return answers


def token_count(value):
    """A non-negative int token count (not bool) of at most 2**53, else None."""
    return (value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53
            else None)


def usage_of(cfg, response):
    """{input_tokens, output_tokens, usd, cost_source} from a response's `usage`, or None.

    A finite non-negative `usage.cost` is "reported"; otherwise USD is "estimated"
    from the configured per-million prices. Missing or invalid token counts give None.
    """
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    tokens_in, tokens_out = token_count(usage.get("input_tokens")), token_count(usage.get("output_tokens"))
    if tokens_in is None or tokens_out is None:
        return None
    cost = config_number(usage.get("cost"), 0.0, math.inf)
    if cost is not None:
        usd, source = cost, "reported"
    else:
        usd = (tokens_in * cfg.get("input_usd_per_million", DEFAULTS["input_usd_per_million"])
               + tokens_out * cfg.get("output_usd_per_million", DEFAULTS["output_usd_per_million"])) / 1e6
        source = "estimated"
    return {"input_tokens": tokens_in, "output_tokens": tokens_out,
            "usd": round(usd, 9), "cost_source": source}


def safe_repr(value, limit=80):
    """UTF-8-safe, length-bounded repr of a possibly-malformed logged value.

    repr() escapes lone surrogates and control characters, so the result is
    always encodable as UTF-8 JSON text; long values are truncated with their
    original repr length noted instead of writing the raw (possibly huge) value.
    """
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}... (len {len(text)})"


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


def _restrict_windows_file(path):
    """Remove inherited access from a newly created, still-empty file, leaving only the
    current user (the same approach as relay/relay.py `_restrict_windows_state`)."""
    system_dir = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(system_dir, len(system_dir))
    if not 0 < length < len(system_dir):
        raise OSError("could not locate the Windows system directory")
    system_dir = Path(system_dir.value)
    # A timeout so a hung whoami.exe/icacls.exe can't block the caller past its own
    # deadline; the caller already treats any exception here (including
    # TimeoutExpired) as a best-effort failure.
    identity = subprocess.run([str(system_dir / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, check=True, timeout=MAX_DEADLINE_SECONDS)
    rows = list(csv.reader(identity.stdout.splitlines()))
    sid = rows[0][-1].strip() if rows and rows[0] else ""
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise OSError("could not determine the current Windows user SID")
    subprocess.run([str(system_dir / "icacls.exe"), str(path), "/inheritance:r",
                    "/grant:r", f"*{sid}:F"],
                   capture_output=True, text=True, check=True, timeout=MAX_DEADLINE_SECONDS)


_LOG_ROTATE_LOCK = threading.Lock()


def write_log(cfg, entry, path=None):
    if not cfg.get("log", True):
        return
    try:
        path = Path(path or cfg.get("log_path") or LOG_PATH)
        # ask-jev --each logs from several threads: check and rotate under one lock so
        # a second thread can't rotate the fresh log over the rotated history.
        with _LOG_ROTATE_LOCK:
            if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
                try:
                    os.replace(path, path.with_name(path.name + ".1"))
                except OSError:
                    # e.g. Windows while another hook holds the log open: skip rotation
                    # this time and still append the entry.
                    pass
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND, 0o600)
            created = False
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            if os.name == "nt":
                # os.open's mode doesn't restrict Windows ACLs; do it once, while the
                # new file is still empty. Best effort: entries hold no text or key.
                if created:
                    try:
                        _restrict_windows_file(path)
                    except Exception:
                        pass
            else:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            line = json.dumps(entry, ensure_ascii=False)
            try:
                line.encode("utf-8")
            except UnicodeEncodeError:
                line = json.dumps(entry)  # a lone surrogate from the classifier: escape, don't drop
            handle.write(line + "\n")
    except Exception:
        pass
