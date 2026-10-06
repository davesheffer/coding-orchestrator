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

    def test_ask_past_deadline_skips_classifier(self):
        cfg = self.config({})
        calls, errors = [], []

        def fn(body, key):
            calls.append(body)
            return {"answers": {}}
        self.assertIsNone(jev_client.ask(cfg, "shift", {}, {}, fn, errors=errors,
                                         deadline=time.monotonic() - 0.01))
        self.assertEqual((calls, errors), ([], ["TimeoutError"]))

    def test_ask_timeout_is_capped_by_deadline(self):
        cfg = self.config({"timeout_seconds": 3})
        ok = lambda body, key: {"answers": {}}  # noqa: E731
        with mock.patch.object(jev_client, "call_with_deadline", return_value={"answers": {}}) as call, \
                mock.patch.object(jev_client.time, "monotonic", return_value=100.0):
            self.assertEqual(jev_client.ask(cfg, "shift", {}, {}, ok, deadline=101.5), {})
            self.assertEqual(jev_client.ask(cfg, "shift", {}, {}, ok, deadline=110.0), {})
            self.assertEqual(jev_client.ask(cfg, "shift", {}, {}, ok), {})
        self.assertEqual([c.args[2] for c in call.call_args_list], [1.5, 3.0, 3.0])

    def test_ask_http_timeout_follows_deadline(self):
        cfg = self.config({"timeout_seconds": 3})
        seen = []

        def fake_http(body, run_cfg, key):
            seen.append(jev_client.effective_timeout(run_cfg))
            return {"answers": {}}
        with mock.patch.object(jev_client, "http_classify", fake_http):
            self.assertEqual(jev_client.ask(cfg, "shift", {}, {}, deadline=time.monotonic() + 1.0), {})
        self.assertTrue(0.0 < seen[0] <= 1.0, seen)

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

    def test_ask_is_a_feature_and_bad_prices_fall_back(self):
        self.assertIn("ask", jev_client.FEATURES)
        self.assertTrue(jev_client.feature_enabled(self.config({}), "ask"))
        for bad in ("cheap", True, None, -1, [1], "nan", "inf", 10 ** 400):
            with self.subTest(bad=repr(bad)[:20]):
                cfg = self.config({"input_usd_per_million": bad, "output_usd_per_million": bad})
                self.assertEqual(cfg["input_usd_per_million"], jev_client.DEFAULTS["input_usd_per_million"])
                self.assertEqual(cfg["output_usd_per_million"], jev_client.DEFAULTS["output_usd_per_million"])
                self.assertIsInstance(cfg["output_usd_per_million"], float)
        cfg = self.config({"input_usd_per_million": "1.5", "output_usd_per_million": 2})
        self.assertEqual((cfg["input_usd_per_million"], cfg["output_usd_per_million"]), (1.5, 2.0))

    def test_usage_of(self):
        cfg = {"input_usd_per_million": 2.0, "output_usd_per_million": 10.0}
        self.assertEqual(jev_client.usage_of(cfg, {"usage": {"input_tokens": 100, "output_tokens": 3,
                                                             "cost": 0.25}}),
                         {"input_tokens": 100, "output_tokens": 3, "usd": 0.25, "cost_source": "reported"})
        for cost in (None, "x", -1, float("nan"), True):
            with self.subTest(cost=cost):
                spent = jev_client.usage_of(cfg, {"usage": {"input_tokens": 1_000_000, "output_tokens": 100_000,
                                                            "cost": cost}})
                self.assertEqual(spent, {"input_tokens": 1_000_000, "output_tokens": 100_000, "usd": 3.0,
                                         "cost_source": "estimated"})
        missing = jev_client.usage_of({}, {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}})
        self.assertAlmostEqual(missing["usd"], jev_client.DEFAULTS["input_usd_per_million"])
        for bad in (True, False, -1, 1.5, "10", None):
            with self.subTest(bad=bad):
                self.assertIsNone(jev_client.usage_of(cfg, {"usage": {"input_tokens": bad, "output_tokens": 1}}))
                self.assertIsNone(jev_client.usage_of(cfg, {"usage": {"input_tokens": 1, "output_tokens": bad}}))
        for response in ({}, {"usage": None}, {"usage": [1, 2]}, None, []):
            self.assertIsNone(jev_client.usage_of(cfg, response))

    def test_ask_logs_usage(self):
        log = self.dir / "log.jsonl"
        cfg = {**self.config({}), "log_path": str(log)}
        questions = {"a": {"type": "noul", "instructions": "x"}, "b": {"type": "noul", "instructions": "y"}}
        spent = {}
        response = {"answers": {"a": {}}, "usage": {"input_tokens": 50, "output_tokens": 2, "cost": 0.001}}
        self.assertEqual(jev_client.ask(cfg, "ask", {}, questions, lambda body, key: response, usage=spent),
                         {"a": {}})
        self.assertEqual(spent, {"input_tokens": 50, "output_tokens": 2, "usd": 0.001, "cost_source": "reported"})
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertIn("ts", entry)
        self.assertEqual({k: v for k, v in entry.items() if k != "ts"},
                         {"kind": "usage", "feature": "ask", "questions": 2, "input_tokens": 50,
                          "output_tokens": 2, "usd": 0.001, "cost_source": "reported"})
        log.unlink()
        spent = {}
        self.assertEqual(jev_client.ask(cfg, "ask", {}, questions, lambda body, key: {"answers": {}},
                                        usage=spent), {})
        self.assertEqual(spent, {})
        self.assertFalse(log.exists())

    def test_huge_usage_counts_are_ignored_and_answers_returned(self):
        log = self.dir / "log.jsonl"
        cfg = {**self.config({}), "log_path": str(log)}
        for huge in (2**53 + 1, 10**400):
            with self.subTest(huge=len(str(huge))):
                self.assertIsNone(jev_client.token_count(huge))
                self.assertIsNone(jev_client.usage_of(cfg, {"usage": {"input_tokens": huge, "output_tokens": 1}}))
        self.assertEqual(jev_client.token_count(2**53), 2**53)
        spent = {}
        response = {"answers": {"a": {}}, "usage": {"input_tokens": 10**400, "output_tokens": 1}}
        self.assertEqual(jev_client.ask(cfg, "ask", {}, {}, lambda body, key: response, usage=spent), {"a": {}})
        self.assertEqual(spent, {})
        with mock.patch.object(jev_client, "usage_of", side_effect=OverflowError):
            self.assertEqual(jev_client.ask(cfg, "ask", {}, {}, lambda body, key: response), {"a": {}})
        with mock.patch.object(jev_client, "write_log", side_effect=OSError):
            ok = {"answers": {"a": {}}, "usage": {"input_tokens": 1, "output_tokens": 1}}
            self.assertEqual(jev_client.ask(cfg, "ask", {}, {}, lambda body, key: ok), {"a": {}})
        self.assertFalse(log.exists())

    def test_user_log_path_is_ignored(self):
        cfg = self.config({"log_path": str(self.dir / "elsewhere.jsonl")})
        self.assertNotIn("log_path", cfg)
        jev_client.write_log({"log_path": 12345j}, {"x": 1})  # bad path type: no raise

    def test_bad_ask_timeout_falls_back(self):
        for bad in ("x", True, None, 0, 0.05, 61, -1, 10**400, "nan", [5]):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertEqual(self.config({"ask_timeout_seconds": bad})["ask_timeout_seconds"], 20)
        self.assertEqual(self.config({"ask_timeout_seconds": 45})["ask_timeout_seconds"], 45.0)
        self.assertEqual(self.config({"ask_timeout_seconds": "0.1"})["ask_timeout_seconds"], 0.1)

    def test_ask_timeout_cap(self):
        ok = lambda body, key: {"answers": {}}  # noqa: E731
        with mock.patch.object(jev_client, "call_with_deadline", return_value={"answers": {}}) as call:
            cfg = self.config({"timeout_seconds": 20})
            self.assertEqual(jev_client.ask(cfg, "ask", {}, {}, ok, timeout_cap=60), {})
            self.assertEqual(jev_client.ask(cfg, "ask", {}, {}, ok), {})
            cfg = self.config({"timeout_seconds": 500})
            self.assertEqual(jev_client.ask(cfg, "ask", {}, {}, ok, timeout_cap=1000), {})
        self.assertEqual([c.args[2] for c in call.call_args_list],
                         [20.0, jev_client.MAX_DEADLINE_SECONDS, jev_client.MAX_ASK_DEADLINE_SECONDS])
        self.assertEqual(jev_client.MAX_ASK_DEADLINE_SECONDS, 60.0)

    def test_api_key_file_must_hold_a_single_token(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            good = self.dir / "key"
            good.write_text("  tok-123  \n", encoding="utf-8")
            self.assertEqual(jev_client.api_key({"api_key_file": str(good)}), "tok-123")
            for text in ("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n",
                         "machine example.com login a password b", "x" * 513, ""):
                bad = self.dir / "bad"
                bad.write_text(text, encoding="utf-8")
                self.assertIsNone(jev_client.api_key({"api_key_file": str(bad)}), text[:20])

    def test_resolve_executable_skips_cwd_and_relative_path_entries(self):
        name = "jevtool"
        exe = name + (".exe" if os.name == "nt" else "")
        planted = self.dir / "cwd"
        planted.mkdir()
        (planted / exe).write_bytes(b"")
        real = self.dir / "bin"
        real.mkdir()
        (real / exe).write_bytes(b"")
        os.chmod(real / exe, 0o755)
        os.chmod(planted / exe, 0o755)
        old = os.getcwd()
        os.chdir(planted)
        self.addCleanup(os.chdir, old)
        env = {"PATH": os.pathsep.join([".", "", str(real)]), "PATHEXT": ".COM;.EXE"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(Path(jev_client.resolve_executable(name)), real / exe)
        with mock.patch.dict(os.environ, {"PATH": ".", "PATHEXT": ".COM;.EXE"}):
            self.assertIsNone(jev_client.resolve_executable(name))


if __name__ == "__main__":
    unittest.main()
