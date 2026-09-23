import json
import os
import sys
import tempfile
import time
import unittest
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

    def test_noul_rejects_malformed(self):
        for answers in (None, {}, {"q": {}}, {"q": {"noul": "x"}}, {"q": {"noul": 1.5}}):
            self.assertIsNone(jev_client.noul(answers, "q"))


if __name__ == "__main__":
    unittest.main()
