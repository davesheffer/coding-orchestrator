import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "jev-route.py"
spec = importlib.util.spec_from_file_location("jev_route", SCRIPT)
jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jev)


UNICODE_TEXT = "\u05e9\u05dc\u05d5\u05dd \u05d0\u05da \U0001f600 \u201cquoted\u201d"
DRIVER = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("jev_route", sys.argv[1])
jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jev)
import jev_client
cfg = jev.load_config(sys.argv[2])
cfg["enabled"] = True
bodies, logs = [], []
jev.load_config = lambda: cfg
jev.write_log = lambda cfg, entry: logs.append(entry)
jev_client.http_classify = lambda body, cfg, key: (
    bodies.append(body), {"answers": {"model": {"choice": "opus", "confidence": 0.9}}})[1]
jev.main()
sys.stderr.write(json.dumps({"bodies": bodies, "logs": logs}))
"""


def utf8_env(encoding=None):
    """Environment without UTF-8 overrides, so stdin uses the locale (or `encoding`)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    if encoding:
        env["PYTHONIOENCODING"] = encoding
    return env


def response(choice, confidence):
    return {"model": "jev-1.13.0", "answers": {"model": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": {choice: confidence}}}, "usage": {}}


class DecideTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        self.payload = {"tool_name": "Agent", "session_id": "s", "tool_input": {
            "description": "Fix parser", "prompt": "Refactor the parser module",
            "subagent_type": "builder", "model": "sonnet", "run_in_background": False}}
        self.calls = []
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "claude-home"
        self.project = Path(temp.name) / "project"

    def decide(self, payload, cfg, classify_fn, log_fn=None):
        return jev.decide(payload, cfg, classify_fn, log_fn, home=self.home)

    def agent(self, root, name, model):
        path = root / "agents" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\nmodel: {model}\n---\nbody\n", encoding="utf-8")

    def classify(self, choice="opus", confidence=0.82):
        def fn(body, key):
            self.calls.append((body, key))
            return response(choice, confidence)
        return fn

    def test_applies_above_threshold_and_preserves_input(self):
        logs = []
        out = self.decide(self.payload, self.cfg, self.classify(), logs.append)
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook["permissionDecision"], "allow")
        self.assertEqual(hook["permissionDecisionReason"], "jev: sonnet → opus (conf 0.82)")
        self.assertEqual(hook["updatedInput"], {**self.payload["tool_input"], "model": "opus"})
        body, key = self.calls[0]
        self.assertEqual(key, "test-key")
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["state"]["prompt"], "Refactor the parser module")
        self.assertEqual(set(body["questions"]["model"]["criteria"]), {"sonnet", "opus", "fable"})
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0]["applied"])
        self.assertNotIn("prompt", logs[0])
        self.assertNotIn("test-key", json.dumps(logs[0]))
        self.assertEqual(logs[0]["desc_hash"],
                         jev.hashlib.sha256(b"Fix parser").hexdigest()[:12])
        self.assertNotIn("description", logs[0])
        self.assertNotIn("Fix parser", json.dumps(logs[0]))

    def test_log_omits_desc_hash_when_description_empty(self):
        self.payload["tool_input"]["description"] = ""
        logs = []
        self.decide(self.payload, self.cfg, self.classify(), logs.append)
        self.assertNotIn("desc_hash", logs[0])
        self.assertNotIn("description", logs[0])

    def test_below_threshold_is_noop(self):
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(confidence=0.3)))

    def test_pinned_critic_is_noop(self):
        self.payload["tool_input"]["subagent_type"] = "critic"
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify()))
        self.assertEqual(self.calls, [])

    def test_missing_key_is_noop(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertIsNone(self.decide(self.payload, self.cfg, self.classify()))
        self.assertEqual(self.calls, [])

    def test_api_key_file_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "key"
            path.write_text("file-key\n")
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                self.assertEqual(jev.api_key({**self.cfg, "api_key_file": str(path)}), "file-key")

    def test_respect_explicit_model(self):
        cfg = {**self.cfg, "respect_explicit_model": True}
        self.assertIsNone(self.decide(self.payload, cfg, self.classify()))
        del self.payload["tool_input"]["model"]
        out = self.decide(self.payload, cfg, self.classify())
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "opus")

    def test_invalid_label_is_noop(self):
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(choice="gpt")))

    def test_same_model_is_noop(self):
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(choice="sonnet")))

    def test_classifier_exception_is_noop(self):
        def boom(body, key):
            raise TimeoutError("slow")
        self.assertIsNone(self.decide(self.payload, self.cfg, boom))

    def test_send_prompt_false_omits_prompt(self):
        cfg = {**self.cfg, "send_prompt": False}
        self.decide(self.payload, cfg, self.classify())
        self.assertEqual(self.calls[0][0]["state"],
                         {"subagent_type": "builder", "description": "Fix parser"})

    def test_non_agent_tool_is_noop(self):
        self.payload["tool_name"] = "Bash"
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify()))

    def load(self, jev_config):
        path = self.home / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jev": {"enabled": True, **jev_config}}), encoding="utf-8")
        return jev.load_config(path)

    def test_config_drops_unknown_labels(self):
        cfg = self.load({"labels": {"gpt": "y", "haiku": "tiny"}, "min_confidence": 0.9})
        self.assertEqual(set(cfg["labels"]), {"sonnet", "opus", "fable", "haiku"})
        self.assertEqual(cfg["min_confidence"], 0.9)
        self.assertEqual(cfg["pinned_agents"], ["critic", "fork"])

    def test_partial_label_override_keeps_other_defaults(self):
        cfg = self.load({"labels": {"opus": "custom opus rubric"}})
        self.assertEqual(cfg["labels"]["opus"], "custom opus rubric")
        self.assertEqual(cfg["labels"]["sonnet"], jev.DEFAULTS["labels"]["sonnet"])
        self.assertEqual(cfg["labels"]["fable"], jev.DEFAULTS["labels"]["fable"])

    def test_null_label_removes_tier(self):
        cfg = self.load({"labels": {"fable": None}})
        self.assertEqual(set(cfg["labels"]), {"sonnet", "opus"})
        self.assertIsNone(self.decide(self.payload, cfg, self.classify(choice="fable")))

    def test_non_list_pinned_agents_is_ignored(self):
        cfg = self.load({"pinned_agents": "critic-builder"})
        self.assertEqual(cfg["pinned_agents"], [])
        self.assertIsNotNone(self.decide(self.payload, cfg, self.classify()))

    def test_slow_classifier_hits_deadline(self):
        cfg = {**self.cfg, "timeout_seconds": 0.2}

        def slow(body, key):
            time.sleep(2)
            return response("opus", 0.9)
        started = time.monotonic()
        self.assertIsNone(self.decide(self.payload, cfg, slow))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_deadline_is_capped_below_hook_timeout(self):
        self.assertEqual(jev.effective_timeout({"timeout_seconds": 30}), 4.0)
        self.assertEqual(jev.effective_timeout({"timeout_seconds": 1.5}), 1.5)
        self.assertEqual(jev.effective_timeout({"timeout_seconds": "bad"}), 4.0)

    def test_endpoint_must_be_https_or_loopback_http(self):
        for endpoint in ("https://api.typesafe.ai/v1/systemone", "http://localhost:8080/x",
                         "http://127.0.0.1/x", "http://[::1]:9/x"):
            self.assertTrue(jev.endpoint_allowed(endpoint), endpoint)
        for endpoint in ("http://api.typesafe.ai/v1/systemone", "ftp://localhost/x",
                         "http://localhost.evil.com/x", "file:///etc/passwd", "https://", None):
            self.assertFalse(jev.endpoint_allowed(endpoint), endpoint)
        cfg = {**self.cfg, "endpoint": "http://api.typesafe.ai/v1/systemone"}
        self.assertIsNone(self.decide(self.payload, cfg, self.classify()))
        self.assertEqual(self.calls, [])

    def test_current_model_from_project_then_user_agent_frontmatter(self):
        del self.payload["tool_input"]["model"]
        self.payload["cwd"] = str(self.project)
        self.agent(self.home, "builder", "opus")
        logs = []
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(choice="opus"), logs.append))
        self.assertEqual(logs[-1]["current"], "opus")
        self.agent(self.project / ".claude", "builder", "sonnet")
        out = self.decide(self.payload, self.cfg, self.classify(choice="opus"), logs.append)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         "jev: sonnet \u2192 opus (conf 0.82)")
        self.assertNotIn("model", self.payload["tool_input"])

    def test_bom_frontmatter_is_read(self):
        del self.payload["tool_input"]["model"]
        path = self.home / "agents" / "builder.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xef\xbb\xbf---\r\nname: builder\r\nmodel: opus\r\n---\r\nbody\r\n")
        logs = []
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(choice="opus"), logs.append))
        self.assertEqual(logs[-1]["current"], "opus")
        self.assertTrue(logs[-1]["reason"].endswith("skipped: same model"))

    def test_model_tier_normalises_full_ids(self):
        tiers = {"sonnet": "", "opus": "", "haiku": "", "fable": ""}
        for model, tier in (("opus", "opus"), ("claude-opus-4-1", "opus"),
                            ("claude-sonnet-4-5-20250929", "sonnet"), ("claude-3-5-haiku-latest", "haiku"),
                            ("us.anthropic.claude-opus-4-1-20250805-v1:0", "opus"),
                            ("gpt-5", "gpt-5"), ("opus-like", "opus-like"), (None, None)):
            self.assertEqual(jev.model_tier(model, tiers), tier, model)
        self.assertEqual(jev.model_tier("claude-3-5-haiku-latest", self.cfg["labels"]),
                         "claude-3-5-haiku-latest")

    def test_full_model_id_of_same_tier_is_not_rewritten(self):
        self.payload["tool_input"]["model"] = "claude-opus-4-1"
        logs = []
        self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(choice="opus"), logs.append))
        self.assertEqual(logs[-1]["current"], "claude-opus-4-1")
        self.assertTrue(logs[-1]["reason"].endswith("skipped: same model"))
        self.payload["tool_input"]["model"] = "claude-sonnet-4-5-20250929"
        out = self.decide(self.payload, self.cfg, self.classify(choice="opus"))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "opus")

    def test_invalid_confidence_is_logged_not_applied(self):
        for value, logged in ((float("nan"), None), (float("inf"), None), (-1, -1.0), (2, 2.0), (None, None)):
            with self.subTest(value=value):
                logs = []
                self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(confidence=value),
                                              logs.append))
                self.assertEqual(len(logs), 1)
                self.assertFalse(logs[0]["applied"])
                self.assertEqual(logs[0]["confidence"], logged)
                self.assertEqual(logs[0]["reason"],
                                 "jev: sonnet \u2192 opus (conf invalid); skipped: invalid confidence")
                json.dumps(logs[0]["confidence"], allow_nan=False)

    def test_unavailable_is_logged_without_secrets(self):
        def boom(body, key):
            raise TimeoutError("slow test-key")
        logs = []
        self.assertIsNone(self.decide(self.payload, self.cfg, boom, logs.append))
        self.assertEqual(len(logs), 1)
        entry = logs[0]
        self.assertEqual((entry["applied"], entry["reason"], entry["error"]),
                         (False, "unavailable", "TimeoutError"))
        self.assertEqual(entry["subagent_type"], "builder")
        self.assertIsInstance(entry["latency_ms"], int)
        self.assertEqual(entry["desc_hash"], jev.hashlib.sha256(b"Fix parser").hexdigest()[:12])
        for secret in ("test-key", "Refactor the parser", "Fix parser"):
            self.assertNotIn(secret, json.dumps(entry))
        logs.clear()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertIsNone(self.decide(self.payload, self.cfg, self.classify(), logs.append))
        self.assertEqual(logs[0]["error"], "NoApiKey")
        logs.clear()
        self.assertIsNone(self.decide(self.payload, {**self.cfg, "enabled": False}, self.classify(), logs.append))
        self.assertEqual(logs, [])

    def test_surrogate_description_still_logs(self):
        self.payload["tool_input"]["description"] = "echo \udc9c"
        logs = []
        self.assertIsNotNone(self.decide(self.payload, self.cfg, self.classify(), logs.append))
        self.assertEqual(len(logs[0]["desc_hash"]), 12)

    def test_current_model_ignores_invalid_frontmatter_and_names(self):
        del self.payload["tool_input"]["model"]
        self.agent(self.home, "builder", "gpt-9")
        logs = []
        self.assertIsNotNone(self.decide(self.payload, self.cfg, self.classify(), logs.append))
        self.assertEqual(logs[-1]["current"], "inherit")
        self.assertIsNone(jev.current_model({}, {"subagent_type": "../builder"}, self.home))


class LogTests(unittest.TestCase):
    def test_log_rotates_over_limit_and_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jev-log.jsonl"
            path.write_bytes(b"x" * (jev.MAX_LOG_BYTES + 1))
            os.chmod(path, 0o644)
            jev.write_log({"log": True}, {"choice": "opus"}, path)
            self.assertEqual(path.with_name("jev-log.jsonl.1").stat().st_size, jev.MAX_LOG_BYTES + 1)
            self.assertEqual(json.loads(path.read_text()), {"choice": "opus"})
            os.chmod(path, 0o644)
            jev.write_log({"log": True}, {"choice": "sonnet"}, path)
            self.assertEqual(len(path.read_text().splitlines()), 2)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            jev.write_log({"log": False}, {"choice": "fable"}, path)
            self.assertEqual(len(path.read_text().splitlines()), 2)


class SubprocessTests(unittest.TestCase):
    def test_no_key_prints_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        payload = {"tool_name": "Agent", "tool_input": {"prompt": "x", "subagent_type": "scout"}}
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_utf8_stdin_is_decoded_whatever_the_locale(self):
        payload = {"tool_name": "Agent", "tool_input": {
            "description": UNICODE_TEXT, "prompt": UNICODE_TEXT, "subagent_type": "general-purpose"}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for encoding in (None, "cp1252"):
            with self.subTest(encoding=encoding):
                env = utf8_env(encoding) | {"TYPESAFE_API_KEY": "test-key"}
                result = subprocess.run([sys.executable, "-c", DRIVER, str(SCRIPT), "/nonexistent/config.json"],
                                        input=data, env=env, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                seen = json.loads(result.stderr.decode("ascii"))
                self.assertEqual(seen["bodies"][0]["state"]["description"], UNICODE_TEXT)
                self.assertEqual(seen["bodies"][0]["state"]["prompt"], UNICODE_TEXT)
                self.assertEqual(seen["logs"][0]["desc_hash"],
                                 jev.hashlib.sha256(UNICODE_TEXT.encode("utf-8")).hexdigest()[:12])
                out = json.loads(result.stdout.decode("ascii"))
                self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["description"], UNICODE_TEXT)


if __name__ == "__main__":
    unittest.main()
