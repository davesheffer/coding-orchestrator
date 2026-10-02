import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import jev_client  # noqa: E402

spec = importlib.util.spec_from_file_location("ask_jev", ROOT / "bin" / "ask-jev.py")
ask_jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ask_jev)

NOUL = json.dumps({"q": {"type": "noul", "instructions": "Does a file validate tokens?"}})


class AskJevTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        self.outside = self.dir / "outside"
        self.outside.mkdir()
        self.log = self.dir / "jev-log.jsonl"
        self.calls = []
        self.lock = threading.Lock()

    def cfg(self, **overrides):
        cfg = jev_client.load_config(self.dir / "missing.json")
        return {**cfg, "enabled": True, "log_path": str(self.log), **overrides}

    def file(self, rel, content="def check(token): return token\n"):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def fake(self, body, key):
        with self.lock:
            self.calls.append(body)
        return {"answers": {"q": {"type": "noul", "noul": 0.8}},
                "usage": {"input_tokens": 100, "output_tokens": 2, "cost": 0.001}}

    def run_args(self, argv, classify_fn=None, cfg=None):
        args = ask_jev.build_parser().parse_args(argv)
        return ask_jev.run(args, cfg or self.cfg(), classify_fn or self.fake, cwd=str(self.repo))

    def test_combined_mode(self):
        self.file("src/a.py")
        self.file("b.txt", "hello\n")
        code, output, entry = self.run_args(["-q", NOUL, "src/a.py", "b.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(set(output), {"answers", "files", "root", "skipped", "usage"})
        self.assertEqual(output["files"], ["b.txt", "src/a.py"])
        self.assertEqual(output["root"], str(self.repo.resolve()))
        self.assertEqual(output["skipped"], {"count": 0, "by_reason": {}, "sample": []})
        self.assertEqual(output["answers"], {"q": {"type": "noul", "noul": 0.8}})
        self.assertEqual(output["usage"], {"calls": 1, "input_tokens": 100, "output_tokens": 2, "usd": 0.001,
                                           "unknown_cost_calls": 0})
        self.assertEqual(len(self.calls), 1)
        state = self.calls[0]["state"]
        self.assertEqual(set(state), {"files"})
        self.assertEqual(sorted(state["files"]), ["b.txt", "src/a.py"])
        self.assertEqual(state["files"]["b.txt"], "hello\n")
        self.assertEqual(entry["decision"], "answered")
        usage_lines = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["kind"], e["feature"]) for e in usage_lines], [("usage", "ask")])

    def test_skip_reasons(self):
        self.file("good.py")
        (self.outside / "far.py").write_text("x = 1\n", encoding="utf-8")
        os.symlink(self.outside / "far.py", self.repo / "link_out.py")
        self.file(".env", "KEY=1\n")
        self.file("secrets/x.txt", "x\n")
        self.file("node_modules/x.js", "x\n")
        self.file("blob.dat", b"abc\0def")
        self.file("empty.txt", "  \n")
        self.file("logo.png", "not really a png\n")
        os.symlink(self.repo / ".env", self.repo / "notes.txt")
        paths = [str(self.outside / "far.py"), "link_out.py", ".env", "secrets/x.txt", "node_modules/x.js",
                 "blob.dat", "empty.txt", "logo.png", "missing.py", "notes.txt", "good.py"]
        code, output, _ = self.run_args(["-q", NOUL, *paths])
        self.assertEqual(code, 0)
        self.assertEqual(output["files"], ["good.py"])
        self.assertEqual(output["skipped"]["count"], 10)
        reasons = {s["path"]: s["reason"] for s in output["skipped"]["sample"]}
        self.assertEqual(set(reasons), set(paths) - {"good.py"})
        self.assertEqual(reasons[str(self.outside / "far.py")], "outside the repository")
        self.assertEqual(reasons["link_out.py"], "outside the repository")
        for secret in (".env", "secrets/x.txt", "notes.txt"):
            self.assertIn("secret-looking", reasons[secret], secret)
        self.assertIn("generated or dependency", reasons["node_modules/x.js"])
        self.assertEqual(reasons["blob.dat"], "binary file")
        self.assertEqual(reasons["empty.txt"], "empty")
        self.assertIn("binary or lock", reasons["logo.png"])
        self.assertEqual(reasons["missing.py"], "not found")
        self.assertEqual(list(self.calls[0]["state"]["files"]), ["good.py"])

    def test_only_unusable_files_is_usage_error(self):
        self.file(".env", "KEY=1\n")
        with self.assertRaises(ask_jev.UsageError):
            self.run_args(["-q", NOUL, ".env"])
        self.assertEqual(self.calls, [])

    def test_secret_text_is_scrubbed(self):
        token = "ghp_" + "a1B2c3D4e5" * 3
        self.file("config.py", f"TOKEN = '{token}'\nprint(TOKEN)\n")
        code, _, _ = self.run_args(["-q", NOUL, "config.py"])
        self.assertEqual(code, 0)
        sent = self.calls[0]["state"]["files"]["config.py"]
        self.assertNotIn(token, sent)
        self.assertIn("[redacted]", sent)
        self.assertNotIn(token, json.dumps(self.calls[0]))

    def test_question_validation(self):
        bad = [
            "[1]", "{}", "not json",
            json.dumps({"q": "text"}),
            json.dumps({"q": {"type": "yesno", "instructions": "x"}}),
            json.dumps({"q": {"type": "noul", "instructions": "  "}}),
            json.dumps({"q": {"type": "noul"}}),
            json.dumps({"q": {"type": "score", "instructions": "x", "criteria": ["only"]}}),
            json.dumps({"q": {"type": "choice", "instructions": "x", "criteria": ["a", "b"]}}),
            json.dumps({"q": {"type": "choice", "instructions": "x", "criteria": {}}}),
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ask_jev.UsageError):
                    ask_jev.parse_questions(raw)
        parsed = ask_jev.parse_questions(json.dumps({
            "a": {"type": "choice", "instructions": "x", "criteria": {"h": "handler", "u": None}},
            "b": {"type": "choice", "instructions": "x", "criteria": {"h": "handler", "None": "neither"}},
            "c": {"type": "score", "instructions": "x", "criteria": ["low", "high"]}}))
        self.assertEqual(parsed["a"]["criteria"], {"h": "handler", "u": None, "other": "None of the above"})
        self.assertEqual(parsed["b"]["criteria"], {"h": "handler", "None": "neither"})
        self.assertEqual(parsed["c"]["criteria"], ["low", "high"])

    def test_combined_file_cap(self):
        for i in range(21):
            self.file(f"f{i:02d}.txt", f"file {i}\n")
        code, output, _ = self.run_args(["-q", NOUL, "*.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(len(output["files"]), 20)
        self.assertEqual(output["skipped"]["count"], 1)
        self.assertEqual(output["skipped"]["sample"][0]["path"], "f20.txt")
        self.assertIn("over the 20-file cap", output["skipped"]["sample"][0]["reason"])
        self.assertIn("--each", output["skipped"]["sample"][0]["reason"])

    def test_combined_over_budget_suggests_split(self):
        self.file("big1.txt", "word " * 30_000)
        self.file("big2.txt", "text " * 30_000)
        with self.assertRaises(ask_jev.UsageError) as caught:
            self.run_args(["-q", NOUL, "big1.txt", "big2.txt"])
        self.assertIn("Split into 2 calls", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_each_mode(self):
        for name in ("a.py", "b.py", "c.py"):
            self.file(name, f"# {name}\n")

        def flaky(body, key):
            if body["state"]["path"] == "b.py":
                raise OSError("down")
            return self.fake(body, key)

        code, output, entry = self.run_args(["-q", NOUL, "--each", "a.py", "b.py", "c.py"], flaky)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(c["state"]["path"] for c in self.calls), ["a.py", "c.py"])
        for call in self.calls:
            self.assertEqual(set(call["state"]), {"path", "content"})
            self.assertEqual(call["state"]["content"], f"# {call['state']['path']}\n")
        self.assertEqual([r["path"] for r in output["results"]], ["a.py", "c.py"])
        self.assertEqual(output["skipped"]["count"], 1)
        self.assertEqual(output["skipped"]["by_reason"], {"jev call failed: OSError": 1})
        self.assertEqual(output["skipped"]["sample"][0]["path"], "b.py")
        self.assertIn("jev call failed", output["skipped"]["sample"][0]["reason"])
        self.assertEqual(output["usage"]["calls"], 2)
        self.assertEqual(entry["decision"], "partial")
        self.assertEqual(entry["errors"], ["OSError"])

    def test_each_mode_calls_once_per_file(self):
        for name in ("a.py", "b.py", "c.py"):
            self.file(name, f"# {name}\n")
        code, output, _ = self.run_args(["-q", NOUL, "--each", "a.py", "b.py", "c.py"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(c["state"]["path"] for c in self.calls), ["a.py", "b.py", "c.py"])
        self.assertEqual(len(output["results"]), 3)

    def test_each_mode_all_failing_exits_3(self):
        self.file("a.py")
        self.file("b.py")

        def boom(body, key):
            raise OSError("down")

        code, output, entry = self.run_args(["-q", NOUL, "--each", "a.py", "b.py"], boom)
        self.assertEqual(code, 3)
        self.assertEqual(output["results"], [])
        self.assertEqual(entry["decision"], "unavailable")

    def test_each_with_stdin_is_usage_error(self):
        self.file("a.py")
        with self.assertRaises(ask_jev.UsageError):
            self.run_args(["-q", NOUL, "--each", "--stdin", "a.py"])
        with self.assertRaises(ask_jev.UsageError):
            self.run_args(["-q", NOUL, "--each", "--state", "ctx", "a.py"])

    def test_disabled_exits_3_without_calling(self):
        self.file("a.py")
        code, output, entry = self.run_args(["-q", NOUL, "a.py"], cfg=self.cfg(enabled=False))
        self.assertEqual(code, 3)
        self.assertIn("Jev is off", output["error"])
        self.assertIsNone(entry)
        code, _, _ = self.run_args(["-q", NOUL, "a.py"], cfg=self.cfg(features={"ask": False}))
        self.assertEqual(code, 3)
        self.assertEqual(self.calls, [])

    def test_no_api_key_exits_3(self):
        self.file("a.py")
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, output, _ = self.run_args(["-q", NOUL, "a.py"],
                                            cfg=self.cfg(api_key_file=None, api_key_source=None))
        self.assertEqual(code, 3)
        self.assertIn("no Jev API key", output["error"])
        self.assertEqual(self.calls, [])

    def test_outside_root_bases_are_not_walked(self):
        self.file("a.py")
        (self.outside / "far.py").write_text("x = 1\n", encoding="utf-8")
        with mock.patch.object(ask_jev.os, "walk", wraps=os.walk) as walk:
            code, output, _ = self.run_args(["-q", NOUL, "a.py", "../outside/*.py", "../**/*.py"])
            self.assertEqual(code, 0)
            self.assertEqual(output["files"], ["a.py"])
            self.assertEqual(output["skipped"]["by_reason"], {"outside the repository": 2})
            self.assertEqual(walk.call_count, 0)
            code, output, _ = self.run_args(["-q", NOUL, "--recursive", "a.py", ".."])
            self.assertEqual(code, 0)
            self.assertEqual(output["skipped"]["sample"], [{"path": "..", "reason": "outside the repository"}])
            self.assertEqual(walk.call_count, 0)
        sent = json.dumps(self.calls)
        self.assertNotIn("far.py", sent)

    def test_glob_prunes_dependency_dirs_and_bounds_skipped(self):
        self.file("src/app.js", "x\n")
        for i in range(30):
            self.file(f"node_modules/pkg{i}/index.js", "x\n")
        for i in range(15):
            self.file(f"lib/.env{i}.js", "x\n")  # secret-looking names, skipped by check_file
        code, output, _ = self.run_args(["-q", NOUL, "**/*.js"])
        self.assertEqual(code, 0)
        self.assertEqual(output["files"], ["src/app.js"])
        self.assertFalse(any("node_modules" in s["path"] for s in output["skipped"]["sample"]))
        self.assertEqual(output["skipped"]["count"], 15)
        self.assertEqual(len(output["skipped"]["sample"]), 10)
        self.assertEqual(sum(output["skipped"]["by_reason"].values()), 15)

    def test_glob_matching(self):
        for rel in ("a.py", "b.txt", "src/c.py", "src/deep/d.py", "src/x1.py", "src/xy.py"):
            self.file(rel, f"# {rel}\n")
        cases = {"*.py": ["a.py"], "src/*.py": ["src/c.py", "src/x1.py", "src/xy.py"],
                 "**/*.py": ["a.py", "src/c.py", "src/deep/d.py", "src/x1.py", "src/xy.py"],
                 "src/**": ["src/c.py", "src/deep/d.py", "src/x1.py", "src/xy.py"],
                 "src/x?.py": ["src/x1.py", "src/xy.py"], "src/x[0-9].py": ["src/x1.py"],
                 "src/x[!0-9].py": ["src/xy.py"], "src/*/d.py": ["src/deep/d.py"]}
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                paths, skipped = ask_jev.expand([pattern], False, str(self.repo), self.repo.resolve())
                self.assertEqual(sorted(paths), expected)
                self.assertEqual(skipped, [])

    def test_glob_extra_stars_match_and_stay_linear(self):
        for rel in ("a.py", "src/deep/d.py"):
            self.file(rel, f"# {rel}\n")
        for pattern in ("**/**/*.py", "***.py", "src/**/**/d.py"):
            with self.subTest(pattern=pattern):
                paths, _ = ask_jev.expand([pattern], False, str(self.repo), self.repo.resolve())
                self.assertEqual(sorted(paths), {"**/**/*.py": ["a.py", "src/deep/d.py"], "***.py": ["a.py", "src/deep/d.py"],
                                                 "src/**/**/d.py": ["src/deep/d.py"]}[pattern])
        deep = "/".join(["d"] * 24) + "/f.py"
        start = time.monotonic()
        for pattern in ("**/" * 10 + "*.zzz", "*" * 40 + ".zzz", "*a" * 30 + "*.zzz"):
            self.assertFalse(ask_jev.translate(pattern).fullmatch(deep))
            self.assertFalse(ask_jev.translate(pattern).fullmatch("a" * 255))
        self.assertLess(time.monotonic() - start, 2)
        with self.assertRaises(ask_jev.UsageError):
            ask_jev.translate("*" * (ask_jev.MAX_PATTERN_CHARS + 1))

    def test_globstar_slash_matches_whole_directories_only(self):
        def reference(pattern):
            out = pattern.replace("**/", "\0").replace("**", "\1").replace("*", "\2")
            out = re.escape(out).replace("\0", "(?:[^/]+/)*").replace("\1", ".*").replace("\2", "[^/]*")
            return re.compile(out.replace("\\?", "[^/]"), re.DOTALL)

        patterns = ["**/a.py", "src/**/test.py", "a/**/", "**/", "**", "a/**", "a/**/**/b", "**/*", "*/**/?.py",
                    "a*/**/b*", "**/**", "?/**/x"]
        paths = ["a.py", "ba.py", "src/test.py", "src/footest.py", "src/deep/test.py", "a/b.py", "a/xb",
                 "a/b", "a/c/b", "a/c/d/b", "ab/b", "ab/x/bc", "x/y.py", "x/yy.py", "q/r/x", "x", "q/x"]
        for pattern in patterns:
            for path in paths:
                with self.subTest(pattern=pattern, path=path):
                    self.assertEqual(ask_jev.translate(pattern).fullmatch(path),
                                     bool(reference(pattern).fullmatch(path)))
        self.assertFalse(ask_jev.translate("src/**/test.py").fullmatch("src/footest.py"))

    def test_bad_glob_class_and_unreadable_dir_are_reported(self):
        self.file("a.py")
        with self.assertRaises(ask_jev.UsageError):
            ask_jev.expand(["[z-a].py"], False, str(self.repo), self.repo.resolve())
        locked = self.repo / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        if os.access(locked, os.R_OK):
            self.skipTest("running as a user that can read mode-000 directories")
        paths, skipped = ask_jev.expand(["locked"], False, str(self.repo), self.repo.resolve())
        self.assertEqual(paths, [])
        self.assertTrue(skipped[0]["reason"].startswith("unreadable"))

    def test_glob_without_globstar_walks_only_its_depth(self):
        self.file("top.py")
        self.file("src/a.py")
        for i in range(10):
            self.file(f"x/d{i}/f.py", "x\n")
        cases = {"*.py": (1, ["top.py"]), "src/*.py": (1, ["src/a.py"]), "*/*.py": (2, ["src/a.py"]),
                 "x/*/f.py": (2, [f"x/d{i}/f.py" for i in range(10)])}
        for pattern, (depth, expected) in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(ask_jev.translate(ask_jev.split_pattern(pattern)[1]).max_depth(), depth)
                with mock.patch.object(ask_jev, "MAX_WALK_ENTRIES", 20):
                    paths, _ = ask_jev.expand([pattern], False, str(self.repo), self.repo.resolve())
                self.assertEqual(sorted(paths), expected)
        for pattern in ("**/*.py", "a/**", "[/]x"):
            self.assertIsNone(ask_jev.translate(pattern).max_depth())
        with mock.patch.object(ask_jev, "MAX_WALK_ENTRIES", 20), self.assertRaises(ask_jev.UsageError):
            ask_jev.expand(["**/*.py"], False, str(self.repo), self.repo.resolve())

    def test_closed_stdin_is_usage_error(self):
        self.file("a.py")
        with mock.patch.object(ask_jev.sys, "stdin", None), self.assertRaises(ask_jev.UsageError):
            self.run_args(["-q", NOUL, "--stdin", "a.py"])

    def test_walk_cap_counts_directories(self):
        self.file("/".join(["e"] * 8) + "/.keep", "")
        with mock.patch.object(ask_jev, "MAX_WALK_ENTRIES", 5):
            with self.assertRaises(ask_jev.UsageError):
                ask_jev.expand(["**/*.py"], False, str(self.repo), self.repo.resolve())

    def test_token_shaped_names_are_not_sent(self):
        token = "ghp_" + "a1B2c3D4e5" * 3
        self.file(f"{token}.py")
        self.file("AKIAIOSFODNN7EXAMPLE/a.py")
        self.file("ok.py")
        code, output, _ = self.run_args(["-q", NOUL, "*.py", "AKIAIOSFODNN7EXAMPLE/a.py"])
        self.assertEqual(code, 0)
        self.assertEqual(output["files"], ["ok.py"])
        self.assertEqual(output["skipped"]["by_reason"], {"secret-looking path; not sent": 2})
        self.assertNotIn(token, json.dumps(self.calls))
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", json.dumps(self.calls))
        for questions in ({token: {"type": "noul", "instructions": "x?"}},
                          {"q": {"type": "choice", "instructions": "x?", "criteria": {token: None}}}):
            with self.subTest(questions=questions), self.assertRaises(ask_jev.UsageError):
                self.run_args(["-q", json.dumps(questions), "ok.py"])

    def test_questions_file(self):
        self.file("a.py")
        outside = self.outside / "qs.json"
        outside.write_text(NOUL, encoding="utf-8")
        code, _, _ = self.run_args(["-q", f"@{outside}", "a.py"])
        self.assertEqual(code, 0)
        with mock.patch.object(ask_jev, "read_questions", side_effect=AssertionError("read")):
            code, _, _ = self.run_args(["-q", f"@{outside}", "a.py"], cfg=self.cfg(enabled=False))
        self.assertEqual(code, 3)
        big = self.outside / "big.json"
        big.write_text(" " * (ask_jev.MAX_QUESTIONS_BYTES + 1), encoding="utf-8")
        secret = self.outside / "secret.json"
        secret.write_text(NOUL, encoding="utf-8")
        for path in (big, secret, self.outside / "missing.json", self.outside):
            with self.subTest(path=path.name), self.assertRaises(ask_jev.UsageError):
                self.run_args(["-q", f"@{path}", "a.py"])

    def test_glob_does_not_follow_symlinked_dirs(self):
        self.file("a.py")
        inner = self.outside / "pkg"
        inner.mkdir()
        (inner / "far.py").write_text("x = 1\n", encoding="utf-8")
        os.symlink(self.outside, self.repo / "linked")
        paths, skipped = ask_jev.expand(["**/*.py"], False, str(self.repo), self.repo.resolve())
        self.assertEqual(paths, ["a.py"])
        paths, _ = ask_jev.expand(["."], True, str(self.repo), self.repo.resolve())
        self.assertEqual(paths, ["a.py"])

    def test_walk_cap(self):
        for i in range(6):
            self.file(f"d/f{i}.txt", f"{i}\n")
        with mock.patch.object(ask_jev, "MAX_WALK_ENTRIES", 5):
            with self.assertRaises(ask_jev.UsageError) as caught:
                self.run_args(["-q", NOUL, "**/*.txt"])
            self.assertIn("more than 5 files", str(caught.exception))
            with self.assertRaises(ask_jev.UsageError):
                self.run_args(["-q", NOUL, "d/f[0-2].txt", "d/f[3-5].txt"])  # counted across patterns
            with self.assertRaises(ask_jev.UsageError):
                self.run_args(["-q", NOUL, "--recursive", "d"])
        self.assertEqual(self.calls, [])

    def test_cap_checked_before_reading(self):
        for i in range(25):
            self.file(f"f{i:02d}.txt", f"file {i}\n")
        with mock.patch.object(ask_jev, "check_file", wraps=ask_jev.check_file) as check:
            code, output, _ = self.run_args(["-q", NOUL, "*.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(check.call_count, 20)
        self.assertEqual(output["skipped"]["count"], 5)

    def test_no_usable_file_message_is_bounded(self):
        for i in range(15):
            self.file(f".env{i}", "KEY=1\n")
        with self.assertRaises(ask_jev.UsageError) as caught:
            self.run_args(["-q", NOUL, *[f".env{i}" for i in range(15)]])
        message = str(caught.exception)
        self.assertIn("15 skipped", message)
        self.assertIn(".env9", message)
        self.assertNotIn(".env10", message)

    def test_outside_git_repository_is_usage_error(self):
        plain = self.dir / "plain"
        plain.mkdir()
        (plain / "a.py").write_text("x\n", encoding="utf-8")
        args = ask_jev.build_parser().parse_args(["-q", NOUL, "a.py"])
        with mock.patch.object(ask_jev, "repo_root", return_value=None):
            with self.assertRaises(ask_jev.UsageError) as caught:
                ask_jev.run(args, self.cfg(), self.fake, cwd=str(plain))
        self.assertIn("not inside a git repository", str(caught.exception))
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.dir)}):
            self.assertIsNone(ask_jev.repo_root(str(plain)))
        self.assertEqual(self.calls, [])

    def test_state_and_questions_are_scrubbed(self):
        token = "ghp_" + "a1B2c3D4e5" * 3
        questions = json.dumps({
            "n": {"type": "noul", "instructions": f"Is {token} used?",
                  "criteria": {"true": f"yes {token}", "false": "no"}},
            "c": {"type": "choice", "instructions": "x", "criteria": {"k": f"has {token}", "j": None}},
            "s": {"type": "score", "instructions": "x", "criteria": [f"low {token}", "high"]}})
        code, _, _ = self.run_args(["-q", questions, "--state", f"context {token}"])
        self.assertEqual(code, 0)
        sent = json.dumps(self.calls[0])
        self.assertNotIn(token, sent)
        self.assertIn("[redacted]", self.calls[0]["state"]["text"])
        self.assertEqual(set(self.calls[0]["questions"]["c"]["criteria"]), {"k", "j", "other"})

    def stdin(self, data):
        return mock.patch.object(ask_jev.sys, "stdin", mock.Mock(buffer=io.BytesIO(data)))

    def test_stdin_limits_and_decoding(self):
        with self.stdin(b"x" * (ask_jev.MAX_FILE_CHARS * 4 + 1)):
            with self.assertRaises(ask_jev.UsageError):
                self.run_args(["-q", NOUL, "--stdin"])
        with self.stdin(b"x" * (ask_jev.MAX_FILE_CHARS + 1)):
            with self.assertRaises(ask_jev.UsageError):
                self.run_args(["-q", NOUL, "--stdin"])
        self.assertEqual(self.calls, [])
        token = "ghp_" + "a1B2c3D4e5" * 3
        with self.stdin(b"bad \xff\xfe bytes " + token.encode()):
            code, _, _ = self.run_args(["-q", NOUL, "--stdin"])
        self.assertEqual(code, 0)
        sent = self.calls[0]["state"]["input"]
        self.assertIn("bad \ufffd\ufffd bytes", sent)
        self.assertNotIn(token, sent)

    def test_main_prints_ascii_json(self):
        self.file("a.py", "caf\u00e9\n")
        out = io.StringIO()
        with mock.patch.object(ask_jev.jev_client, "load_config", return_value=self.cfg()), \
                mock.patch.object(ask_jev, "run", return_value=(0, {"answers": {"q": "caf\u00e9"}}, None)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(ask_jev.main(["-q", NOUL, "a.py"]), 0)
        self.assertTrue(out.getvalue().isascii())
        self.assertEqual(json.loads(out.getvalue()), {"answers": {"q": "caf\u00e9"}})

    def test_main_help_questions(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ask_jev.main(["--help-questions"]), 0)
        self.assertIn("QUESTIONS is a JSON object", out.getvalue())


if __name__ == "__main__":
    unittest.main()
