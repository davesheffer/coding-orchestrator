import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "bin" / "role-guard.py"
RO = ROOT / "bin" / "ro.py"


def run_guard(command=None, raw=None, tool_input=None):
    if raw is None:
        raw = json.dumps({"tool_input": tool_input if tool_input is not None else {"command": command}})
    return subprocess.run([sys.executable, str(GUARD)], input=raw.encode("utf-8"),
                          capture_output=True)


class RoleGuardTests(unittest.TestCase):
    def test_allowed_commands(self):
        ro = RO.as_posix()
        for command in (f"python {ro} log -n 5", f"python3 '{ro}' status",
                        f'python "{ro}" diff --stat HEAD~1..HEAD', f"python {ro} pr 12"):
            with self.subTest(command=command):
                result = run_guard(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"")

    def test_denied_commands(self):
        ro = RO.as_posix()
        for command in ("ls", "cat x", f"python {ro} log; rm x", f"python {ro} log > f",
                        f"python {ro} log | sh", "$(x)", f"python {ro} log `x`",
                        f"GIT_X=1 python {ro} log", "python other.py log", f"python {ro} evil",
                        f"python {ro} log\nrm x", f"env python {ro} log", "python -c 'print(1)'",
                        f"python {ro}", "", f"python {ro} log && ls", f"python {ro} log\x00"):
            with self.subTest(command=command):
                result = run_guard(command)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr.startswith(b"role-guard: denied:"))

    def test_unc_and_home_paths_are_denied_without_resolving(self):
        for command in ("python //host/share/ro.py log", r"python \\\\host\\share\\ro.py log",
                        r"python '\\host\share\ro.py' log", r"python '/\host\share\ro.py' log",
                        r"python '\\?\C:\ro.py' log", "python ~/.claude/bin/ro.py log",
                        "python C:ro.py log", "python 'C:/x/ro.py:s' log"):
            with self.subTest(command=command):
                result = run_guard(command)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr.startswith(b"role-guard: denied:"))

    def test_unc_paths_never_reach_realpath(self):
        spec = importlib.util.spec_from_file_location("guard_unc_module", GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)

        def boom(path):
            raise AssertionError(f"norm called for {path!r}")
        guard.norm = boom
        for path in ("//host/share/ro.py", r"\\host\share\ro.py", "/\\host/ro.py", "\\ro.py",
                     "~/ro.py", "C:ro.py", "C:/a/ro.py:ads", ""):
            with self.subTest(path=path):
                self.assertFalse(guard.local_path(path))
                self.assertFalse(guard.allowed(f"python '{path}' log"))
        for path in ("C:/a/ro.py", "C:\\a\\ro.py", "/a/ro.py", "bin/ro.py", "..\\ro.py"):
            with self.subTest(path=path):
                self.assertTrue(guard.local_path(path))

    def test_backslash_form_of_real_path(self):
        variant = str(RO).replace("/", "\\")
        result = run_guard(f"python '{variant}' log")
        self.assertEqual(result.returncode, 0 if os.name == "nt" else 2, result.stderr)

    def test_path_check_is_lexical_and_absolute(self):
        spec = importlib.util.spec_from_file_location("guard_lexical_module", GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        real = os.path.realpath

        def boom(path):
            raise AssertionError(f"realpath called for {path!r}")
        os.path.realpath = boom
        try:
            ro = RO.as_posix()
            self.assertTrue(guard.allowed(f"python '{ro}' log"))
            self.assertTrue(guard.allowed(f"python '{RO.parent.as_posix()}/sub/../ro.py' log"))
            self.assertFalse(guard.allowed("python bin/ro.py log"))
            self.assertFalse(guard.allowed("python ./ro.py log"))
        finally:
            os.path.realpath = real

    def test_install_path_with_shell_characters(self):
        with tempfile.TemporaryDirectory() as temp:
            bin_dir = Path(temp) / "Dave (Admin) & co!" / "bin"
            bin_dir.mkdir(parents=True)
            for source in (GUARD, RO):
                shutil.copy(source, bin_dir / source.name)
            ro = shlex.quote((bin_dir / "ro.py").as_posix())
            for command, code in ((f"python {ro} status", 0), (f"python {ro} log -n 3", 0),
                                  (f"python {ro} evil", 2), (f"python {ro} log; ls", 2),
                                  (f"python {ro} log $(x)", 2), (f"python {ro}", 2)):
                with self.subTest(command=command):
                    raw = json.dumps({"tool_input": {"command": command}}).encode("utf-8")
                    result = subprocess.run([sys.executable, str(bin_dir / "role-guard.py")],
                                            input=raw, capture_output=True)
                    self.assertEqual(result.returncode, code, result.stderr)

    def test_malformed_input_is_denied(self):
        for kwargs in ({"raw": "not json"}, {"raw": ""}, {"tool_input": {}},
                       {"tool_input": {"command": 5}},
                       {"raw": json.dumps({"tool_input": "x"})}, {"raw": "[]"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(run_guard(**kwargs).returncode, 2)

    def test_verbs_match_ro(self):
        modules = {}
        for name, path in (("guard", GUARD), ("ro", RO)):
            spec = importlib.util.spec_from_file_location(f"{name}_module", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[name] = module
        self.assertEqual(modules["guard"].VERBS, modules["ro"].VERBS)


if __name__ == "__main__":
    unittest.main()
