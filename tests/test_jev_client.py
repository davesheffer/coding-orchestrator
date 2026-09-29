import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import jev_client  # noqa: E402


class ClientTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def config(self, jev):
        path = self.dir / "config.json"
        path.write_text(json.dumps({"jev": {"enabled": True, **jev}}), encoding="utf-8")
        return jev_client.load_config(path)

    def test_disabled_by_default_features_on_and_merge_known_names_only(self):
        cfg = jev_client.load_config(self.dir / "missing.json")
        self.assertTrue(all(cfg["features"][name] for name in jev_client.FEATURES))
        self.assertFalse(any(jev_client.feature_enabled(cfg, name) for name in jev_client.FEATURES))
        cfg = self.config({"features": {"risk_gate": False, "bogus": True}, "shift_low": 0.1})
        self.assertFalse(jev_client.feature_enabled(cfg, "risk_gate"))
        self.assertTrue(jev_client.feature_enabled(cfg, "route"))
        self.assertNotIn("bogus", cfg["features"])
        self.assertEqual(cfg["shift_low"], 0.1)
        self.assertFalse(jev_client.feature_enabled(self.config({"enabled": False}), "route"))

    def test_bad_thresholds_fall_back_to_defaults(self):
        keys = ("min_confidence", "upgrade_min_confidence", "downgrade_min_confidence", "escalate_mass",
                "shift_low", "shift_high", "risk_min_probability", "report_min_support", "report_max_gap")
        self.assertEqual(set(keys), set(jev_client.THRESHOLDS))
        path = self.dir / "config.json"
        for bad in ("high", True, False, None, [0.5], {"v": 0.5}, 10 ** 400, -0.1, 1.5, "nan", "inf"):
            with self.subTest(bad=repr(bad)[:20]):
                cfg = self.config({key: bad for key in keys})
                for key in keys:
                    self.assertEqual(cfg[key], jev_client.DEFAULTS[key], key)
                    self.assertIsInstance(cfg[key], float)
        for literal in ("NaN", "Infinity", "-Infinity"):  # JSON extensions json.loads accepts
            path.write_text('{"jev": {"min_confidence": %s, "risk_min_probability": %s}}' % (literal, literal),
                            encoding="utf-8")
            cfg = jev_client.load_config(path)
            self.assertEqual((cfg["min_confidence"], cfg["risk_min_probability"]), (0.5, 0.6))
        cfg = self.config({"min_confidence": 0, "risk_min_probability": 1, "shift_low": "0.1"})
        self.assertEqual((cfg["min_confidence"], cfg["risk_min_probability"], cfg["shift_low"]), (0.0, 1.0, 0.1))

    def test_bad_handoff_min_score_and_max_prompt_chars_fall_back_to_defaults(self):
        for bad in ("high", True, None, [2], 9, -1, 10 ** 400, "nan"):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertEqual(self.config({"handoff_min_score": bad})["handoff_min_score"], 2)
        for good, expected in ((3, 3), ("3", 3), (2.5, 2.5), (0, 0), (4, 4)):
            self.assertEqual(self.config({"handoff_min_score": good})["handoff_min_score"], expected)
        for bad in ("6000x", True, None, -1, 12.5, [6000], 10 ** 400, "inf"):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertEqual(self.config({"max_prompt_chars": bad})["max_prompt_chars"], 6000)
        for good, expected in ((100, 100), (100.0, 100), ("250", 250), (0, 0)):
            value = self.config({"max_prompt_chars": good})["max_prompt_chars"]
            self.assertEqual((value, type(value)), (expected, int))

    def test_ask_sends_body_and_returns_answers(self):
        calls = []

        def fake(body, key):
            calls.append((body, key))
            return {"answers": {"q": {"type": "noul", "noul": 0.9}}}

        cfg = self.config({})
        questions = {"q": {"type": "noul", "instructions": "x"}}
        answers = jev_client.ask(cfg, "shift", {"a": 1}, questions, fake)
        self.assertEqual(jev_client.noul(answers, "q"), 0.9)
        self.assertEqual(calls, [({"state": {"a": 1}, "model": "jev-latest", "questions": questions},
                                  "test-key")])

    def test_ask_fails_open(self):
        cfg = jev_client.load_config(self.dir / "missing.json")
        ok = lambda body, key: {"answers": {}}  # noqa: E731

        def boom(body, key):
            raise OSError("down")

        def slow(body, key):
            time.sleep(1)
            return {"answers": {}}

        self.assertIsNone(jev_client.ask(cfg, "shift", {}, {}, boom))
        self.assertIsNone(jev_client.ask({**cfg, "timeout_seconds": 0.05}, "shift", {}, {}, slow))
        self.assertIsNone(jev_client.ask(cfg, "shift", {}, {}, lambda body, key: {"nope": 1}))
        self.assertIsNone(jev_client.ask({**cfg, "endpoint": "http://example.com"}, "shift", {}, {}, ok))
        self.assertIsNone(jev_client.ask(self.config({"features": {"shift": False}}), "shift", {}, {}, ok))
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertIsNone(jev_client.ask(cfg, "shift", {}, {}, ok))

    def test_ask_reports_failure_reason(self):
        cfg = self.config({})

        def boom(body, key):
            raise OSError("secret-body")

        def slow(body, key):
            time.sleep(1)
            return {"answers": {}}

        cases = [
            (cfg, boom, ["OSError"]),
            ({**cfg, "timeout_seconds": 0.05}, slow, ["TimeoutError"]),
            (cfg, lambda body, key: {"answers": []}, ["MalformedResponse"]),
            (self.config({"features": {"shift": False}}), boom, []),
        ]
        for config, fn, expected in cases:
            errors = []
            self.assertIsNone(jev_client.ask(config, "shift", {}, {}, fn, errors=errors))
            self.assertEqual(errors, expected)
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            errors = []
            self.assertIsNone(jev_client.ask(cfg, "shift", {}, {}, boom, errors=errors))
            self.assertEqual(errors, ["NoApiKey"])

    def test_write_log_appends_when_rotation_fails(self):
        path = self.dir / "jev-log.jsonl"
        path.write_text("x" * (jev_client.MAX_LOG_BYTES + 1) + "\n", encoding="utf-8")
        with mock.patch.object(jev_client.os, "replace", side_effect=PermissionError("in use")):
            jev_client.write_log({}, {"n": 1}, path)
        self.assertEqual(path.read_text(encoding="utf-8").splitlines()[-1], '{"n": 1}')
        self.assertFalse(path.with_name(path.name + ".1").exists())

    def test_write_log_rotates(self):
        path = self.dir / "jev-log.jsonl"
        path.write_text("x" * (jev_client.MAX_LOG_BYTES + 1) + "\n", encoding="utf-8")
        jev_client.write_log({}, {"n": 1}, path)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"n": 1}\n')
        self.assertTrue(path.with_name(path.name + ".1").exists())

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_write_log_restricts_windows_acl_on_create_only(self):
        path = self.dir / "jev-log.jsonl"
        real = jev_client._restrict_windows_file
        with mock.patch.object(jev_client, "_restrict_windows_file", side_effect=real) as restrict:
            jev_client.write_log({}, {"n": 1}, path)
            jev_client.write_log({}, {"n": 2}, path)
        restrict.assert_called_once_with(path)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
        self.assertNotIn("(I)", acl)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_write_log_mode_0600(self):
        path = self.dir / "jev-log.jsonl"
        jev_client.write_log({}, {"n": 1}, path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_noul_rejects_malformed(self):
        for answers in (None, {}, {"q": {}}, {"q": {"noul": "x"}}, {"q": {"noul": 1.5}}):
            self.assertIsNone(jev_client.noul(answers, "q"))

    def test_probability_is_strict(self):
        for bad in (True, False, "0.5", 10 ** 400, float("nan"), float("inf"), -0.1, 1.5, None, [0.5]):
            self.assertIsNone(jev_client.probability(bad), repr(bad)[:20])
        self.assertEqual([jev_client.probability(v) for v in (0, 1, 0.25)], [0.0, 1.0, 0.25])
        self.assertIs(jev_client.coerce_confidence, jev_client.probability)

    def test_http_classify_disables_redirects(self):
        cfg = self.config({})
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"answers": {}}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(jev_client.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(jev_client.http_classify({}, cfg, "test-key"), {"answers": {}})
        handler_type = build.call_args.args[0]
        self.assertTrue(issubclass(handler_type, urllib.request.HTTPRedirectHandler))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIsNone(handler_type().redirect_request(request, None, 302, "Found", {},
                                                          "https://untrusted.invalid/collect"))


if __name__ == "__main__":
    unittest.main()
