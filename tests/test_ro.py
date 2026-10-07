import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RO = ROOT / "bin" / "ro.py"
_SPEC = importlib.util.spec_from_file_location("ro_module", RO)
ro = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ro)


def run_ro(*args, cwd=None):
    return subprocess.run([sys.executable, str(RO), *args], cwd=cwd, text=True, capture_output=True)


class ArgvTests(unittest.TestCase):
    def test_rejects_bad_arguments(self):
        for verb, args in (("log", ["--output=x"]), ("log", ["-c"]), ("diff", ["--ext-diff"]),
                           ("show", ["-p", "HEAD"]), ("show", ["-x"]), ("log", ["-n", "0"]),
                           ("log", ["-n", "501"]), ("log", ["-n"]), ("log", ["a", "b"]),
                           ("diff", ["a..b..c"]), ("diff", ["--cached=x"]), ("show", []),
                           ("log", ["--", "-bad"]), ("log", ["--", "a\x00b"]), ("blame", []),
                           ("blame", ["-x"]), ("blame", ["f", "-L", "x"]), ("issue", ["abc"]),
                           ("pr", []), ("issues", ["x"]), ("status", ["-s"]), ("evil", [])):
            with self.subTest(verb=verb, args=args):
                with self.assertRaises(ro.UsageError):
                    ro.build_argv(verb, args)

    def test_builds_expected_argv(self):
        base = ro.GIT_BASE
        sig = ["--no-show-signature"]
        sub = ["--ignore-submodules=all", "--submodule=short"]
        self.assertEqual(ro.build_argv("log", []),
                         base + ["log", "--no-textconv", "--no-ext-diff", *sig, *sub, "-n", "20"])
        self.assertEqual(ro.build_argv("log", ["-n", "5", "--oneline", "main", "--", "a b.py"]),
                         base + ["log", "--no-textconv", "--no-ext-diff", *sig, *sub, "-n", "5",
                                 "--oneline", "main", "--", "a b.py"])
        self.assertEqual(ro.build_argv("diff", ["--cached", "--stat", "a...b"]),
                         base + ["diff", "--no-textconv", "--no-ext-diff", *sub, "--cached", "--stat",
                                 "a...b"])
        self.assertEqual(ro.build_argv("show", ["HEAD~1"]),
                         base + ["show", "--no-textconv", "--no-ext-diff", *sig, *sub, "HEAD~1"])
        self.assertEqual(ro.build_argv("status", []),
                         base + ["status", "--short", "--branch", "--ignore-submodules=all"])
        self.assertEqual(ro.build_argv("blame", ["f.py", "-L", "1,5"]),
                         base + ["blame", "--no-textconv", "-L", "1,5", "--", "f.py"])
        self.assertEqual(ro.build_argv("issues", []), ["gh", "issue", "list", "--limit", "30"])
        self.assertEqual(ro.build_argv("prs", []), ["gh", "pr", "list", "--limit", "30"])
        self.assertEqual(ro.build_argv("issue", ["7"]), ["gh", "issue", "view", "7"])
        self.assertEqual(ro.build_argv("pr", ["7"]), ["gh", "pr", "view", "7"])
        self.assertEqual(base[:4], ["git", "--no-pager", "-c", "core.fsmonitor=false"])

    def test_signature_and_submodule_flags(self):
        self.assertIn("--no-show-signature", ro.build_argv("log", []))
        self.assertIn("--no-show-signature", ro.build_argv("show", ["HEAD"]))
        self.assertNotIn("--no-show-signature", ro.build_argv("diff", []))
        for verb, args in (("status", []), ("diff", []), ("log", []), ("show", ["HEAD"])):
            with self.subTest(verb=verb):
                self.assertIn("--ignore-submodules=all", ro.build_argv(verb, args))
        for verb, args in (("diff", []), ("log", []), ("show", ["HEAD"])):
            with self.subTest(verb=verb):
                self.assertIn("--submodule=short", ro.build_argv(verb, args))
        self.assertNotIn("--submodule=short", ro.build_argv("status", []))

    def test_env_scrub(self):
        keep = {"GIT_EXTERNAL_DIFF": "x", "GIT_CONFIG_PARAMETERS": "'a=b'", "GH_TOKEN": "t"}
        old = {k: os.environ.get(k) for k in keep}
        os.environ.update(keep)
        try:
            env = ro.scrubbed_env()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertNotIn("GIT_EXTERNAL_DIFF", env)
        self.assertNotIn("GIT_CONFIG_PARAMETERS", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")

    def test_cli_usage_errors_exit_2(self):
        for args in ((), ("evil",), ("log", "--output=x"), ("log", "-c"), ("diff", "--ext-diff"),
                     ("show", "-bad")):
            with self.subTest(args=args):
                result = run_ro(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        for name in ("git", "git.exe", "git.cmd"):
            planted = self.dir / name
            planted.write_text("planted")
            planted.chmod(0o755)
        (self.dir / "sub").mkdir()
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)

    def test_cwd_and_relative_entries_are_skipped(self):
        path = os.pathsep.join(["", ".", "sub", str(self.dir)])
        with mock.patch.dict(os.environ, {"PATH": path}):
            with self.assertRaises(OSError):
                ro.resolve("git")

    def test_absolute_entry_below_cwd_is_used(self):
        for name in ("git", "git.exe"):
            planted = self.dir / "sub" / name
            planted.write_text("tool")
            planted.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.dir / "sub"), "PATHEXT": ".EXE"}):
            found = Path(ro.resolve("git"))
        self.assertEqual(found.parent, self.dir / "sub")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_real_git_wins_over_planted(self):
        real = Path(shutil.which("git")).resolve().parent
        path = os.pathsep.join([str(self.dir), str(real)])
        with mock.patch.dict(os.environ, {"PATH": path}):
            found = Path(ro.resolve("git"))
        self.assertEqual(found.parent, real)
        self.assertNotEqual(found.resolve().parent, self.dir.resolve())


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RepoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        (self.repo / "f.txt").write_text("hello\n")
        self.git("add", "f.txt")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "first")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def test_log_and_status_work(self):
        result = run_ro("log", "--oneline", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("first", result.stdout)
        self.assertEqual(run_ro("status", cwd=self.repo).returncode, 0)

    def test_local_fsmonitor_is_refused(self):
        marker = self.repo / "PWNED"
        self.git("config", "core.fsmonitor", f"touch {marker.as_posix()}")
        result = run_ro("status", cwd=self.repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ro: refused: repo-local config defines core.fsmonitor", result.stderr)
        self.assertFalse(marker.exists())

    def test_local_gpg_and_show_signature_are_refused(self):
        for key, value in (("gpg.program", "x"), ("log.showSignature", "true")):
            with self.subTest(key=key):
                self.git("config", key, value)
                result = run_ro("log", cwd=self.repo)
                self.assertEqual(result.returncode, 2)
                self.assertIn("refused", result.stderr)
                self.assertIn(key.lower(), result.stderr)
                self.git("config", "--unset", key)

    def test_local_filter_is_refused(self):
        self.git("config", "filter.x.clean", "cat")
        result = run_ro("log", cwd=self.repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("filter.x.clean", result.stderr)


if __name__ == "__main__":
    unittest.main()
