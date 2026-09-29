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


if __name__ == "__main__":
    unittest.main()
