import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "bin" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompareReadonlyTests(unittest.TestCase):
    def test_failed_run_is_recorded_and_completed_runs_are_written(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)

            def run_one(repo, task, model, *budget):
                if model == "bad":
                    raise RuntimeError("launcher crashed")
                return {"task": task["id"], "model": model, "exit": 0}

            with self.subTest(name), \
                    tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                tasks = Path(temp) / "tasks.json"
                tasks.write_text(json.dumps([{"id": "t1", "required": []}]), encoding="utf-8")
                output = Path(temp) / "out" / "results.json"
                with mock.patch.object(module, "run_one", run_one), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = module.main(["--tasks", str(tasks), "--models", "good", "bad",
                                        "--jobs", "1", "--output", str(output)])
                self.assertEqual(code, 1)
                records = sorted(json.loads(output.read_text(encoding="utf-8")),
                                 key=lambda record: record["model"])
                self.assertEqual(records, [
                    {"task": "t1", "model": "bad", "exit": 1,
                     "error": "RuntimeError: launcher crashed"},
                    {"task": "t1", "model": "good", "exit": 0}])

    def test_planted_claude_in_cwd_is_never_resolved(self):
        """Planted files are only inspected, never run."""
        module = load("compare-claude-readonly")
        with tempfile.TemporaryDirectory() as temp:
            cwd, tools = Path(temp) / "repo", Path(temp) / "tools"
            for folder in (cwd, tools, cwd / "sub"):
                folder.mkdir()
            for name in ("claude", "claude.exe", "claude.cmd"):
                (cwd / name).write_text("planted")
                (cwd / name).chmod(0o755)
            path = os.pathsep.join(["", ".", "sub", str(cwd)])
            with contextlib.chdir(cwd), mock.patch.dict(os.environ, {"PATH": path, "PATHEXT": ".EXE;.CMD"}):
                self.assertIsNone(module.resolve_executable("claude"))
                with self.assertRaisesRegex(RuntimeError, "not installed on PATH"):
                    module.executable()
            real = tools / ("claude.cmd" if os.name == "nt" else "claude")
            real.write_text("tool")
            real.chmod(0o755)
            path = os.pathsep.join([".", str(cwd), str(tools)])
            with contextlib.chdir(cwd), mock.patch.dict(os.environ, {"PATH": path, "PATHEXT": ".EXE;.CMD"}):
                found = module.executable()
                self.assertEqual(found.lower(), str(real).lower())
                (tools / "claude.exe").write_text("tool")
                self.assertEqual(module.executable(), found)  # resolved once, then cached

    def test_codex_timeout_kills_the_whole_process_tree(self):
        module = load("compare-codex-readonly")
        process = mock.Mock(pid=4242)
        process.communicate.side_effect = [module.subprocess.TimeoutExpired("agent-run", 180), ("", "")]
        with mock.patch.object(module.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(module.subprocess, "run") as run, \
                mock.patch.object(module.os, "killpg", create=True) as killpg:
            record = module.run_one(Path("."), {"id": "t1", "prompt": "p", "required": []}, "m")
        self.assertEqual(record, {"task": "t1", "model": "m", "exit": 124, "error": "timeout"})
        process.kill.assert_called_once()
        if module.sys.platform == "win32":
            command = run.call_args.args[0]
            self.assertTrue(os.path.isabs(command[0]))
            self.assertTrue(command[0].lower().endswith("taskkill.exe"))
            self.assertEqual(command[1:], ["/T", "/F", "/PID", "4242"])
            self.assertFalse(popen.call_args.kwargs["start_new_session"])
        else:
            killpg.assert_called_once_with(4242, module.signal.SIGKILL)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
