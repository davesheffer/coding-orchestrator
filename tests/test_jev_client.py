import json
import os
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

    def test_noul_rejects_malformed(self):
        for answers in (None, {}, {"q": {}}, {"q": {"noul": "x"}}, {"q": {"noul": 1.5}}):
            self.assertIsNone(jev_client.noul(answers, "q"))

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
