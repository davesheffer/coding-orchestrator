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
                tasks.write_text(json.dumps([{"id": "t1", "prompt": "p", "required": ["alpha"]}]), encoding="utf-8")
                output = Path(temp) / "out" / "results.json"
                with mock.patch.object(module, "run_one", run_one), \
                        mock.patch.object(module, "tree_status", return_value=""), \
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
            real = tools / ("claude.exe" if os.name == "nt" else "claude")
            real.write_text("tool")
            real.chmod(0o755)
            path = os.pathsep.join([".", str(cwd), str(tools)])
            with contextlib.chdir(cwd), mock.patch.dict(os.environ, {"PATH": path, "PATHEXT": ".EXE;.CMD"}):
                found = module.executable()
                self.assertEqual(found.lower(), str(real).lower())
                real.unlink()
                self.assertEqual(module.executable(), found)  # resolved once, then cached

    @unittest.skipUnless(os.name == "nt", "cmd.exe wrappers exist only on Windows")
    def test_windows_never_launches_a_cmd_or_bat_wrapper(self):
        """cmd.exe would execute a `"` + `&` in the prompt; files are planted, never run."""
        for suffix in (".cmd", ".bat"):
            module = load("compare-claude-readonly")
            with self.subTest(suffix), tempfile.TemporaryDirectory() as temp:
                tools = Path(temp) / "tools"
                tools.mkdir()
                (tools / f"claude{suffix}").write_text("@echo off")
                env = {"PATH": str(tools), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
                with mock.patch.dict(os.environ, env), mock.patch.object(module.subprocess, "run") as run:
                    with self.assertRaisesRegex(RuntimeError, "refusing a shell-interpolated launch"):
                        module.run_one(Path(temp), {"id": "t1", "prompt": 'x" & echo pwned > f & "',
                                                    "required": []}, "haiku", .2)
                    run.assert_not_called()
                    # The npm shim only locates the native binary installed beside it.
                    native = tools / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
                    native.parent.mkdir(parents=True)
                    native.write_text("tool")
                    self.assertEqual(module.executable(), str(native))

    @unittest.skipUnless(os.name == "nt", "cmd.exe wrappers exist only on Windows")
    def test_a_stale_cmd_shim_earlier_on_path_does_not_hide_a_real_exe(self):
        module = load("compare-claude-readonly")
        with tempfile.TemporaryDirectory() as temp:
            stale, real = Path(temp) / "stale", Path(temp) / "real"
            for folder in (stale, real):
                folder.mkdir()
            (stale / "claude.cmd").write_text("@echo off")
            (real / "claude.exe").write_text("tool")
            env = {"PATH": os.pathsep.join([str(stale), str(real)]), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(module.executable().lower(), str(real / "claude.exe").lower())

    def claude_record(self, stdout, required=("alpha", "beta"), **task):
        module = load("compare-claude-readonly")
        done = module.subprocess.CompletedProcess([], 0, stdout, "")
        with mock.patch.object(module, "executable", return_value="claude.exe"), \
                mock.patch.object(module.subprocess, "run", return_value=done):
            return module.run_one(Path("."), {"id": "t1", "prompt": "p", "required": list(required), **task},
                                  "haiku", .2)

    def test_claude_output_without_an_answer_is_a_failed_run(self):
        for stdout, error in (("Not logged in", "non-JSON CLI output"), ("", "non-JSON CLI output"),
                              ("[1, 2]", "no answer in CLI output"),
                              ('{"subtype": "error_max_turns"}', "no answer in CLI output"),
                              ('{"result": "  "}', "no answer in CLI output"),
                              ('{"result": 7}', "no answer in CLI output")):
            with self.subTest(stdout=stdout):
                record = self.claude_record(stdout)
                self.assertEqual(record["exit"], 1)
                self.assertEqual(record["error"], error)
                self.assertNotIn("required_hits", record)

    def test_claude_output_that_reports_an_error_is_a_failed_run_even_with_exit_0(self):
        for output, error in (
                ({"is_error": True, "result": "alpha beta: API Error 529"}, "CLI reported an error: alpha beta: API Error 529"),
                ({"is_error": True, "subtype": "error_max_budget_usd", "result": "alpha beta"},
                 "CLI reported an error: error_max_budget_usd"),
                ({"is_error": True}, "CLI reported an error")):
            with self.subTest(output=output):
                record = self.claude_record(json.dumps(output))
                self.assertEqual((record["exit"], record["error"]), (1, error))
                self.assertNotIn("required_met", record)
        self.assertEqual(self.claude_record(json.dumps({"is_error": False, "result": "alpha beta"}))["exit"], 0)

    def test_a_task_that_every_answer_would_pass_is_a_config_error(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            # Graded directly, no hit is never a pass.
            self.assertEqual(module.grade({"required": []}, "anything"), ({}, False))
            self.assertFalse(module.grade({"required": ["alpha"], "min_hits": 0}, "neither")[1])
            self.assertIsNone(module.task_problem({"id": "t1", "prompt": "p", "required": ["alpha", "beta"],
                                                   "min_hits": 1}))
            for task in ({"required": []}, {}, {"required": ["alpha"], "min_hits": 0},
                         {"required": ["alpha"], "min_hits": 2}, {"required": ["alpha"], "min_hits": True},
                         {"required": ["alpha", " "]}, {"required": "alpha"},
                         {"required": ["a", "a"], "min_hits": 2}, {"required": ["Alpha", "alpha"]}):
                with self.subTest(name, task=task), \
                        tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                    tasks = Path(temp) / "tasks.json"
                    tasks.write_text(json.dumps([{"id": "t1", **task}]), encoding="utf-8")
                    problem = io.StringIO()
                    with mock.patch.object(module, "run_one") as run_one, \
                            mock.patch.object(module, "tree_status", return_value=""), \
                            contextlib.redirect_stderr(problem), self.assertRaises(SystemExit) as error:
                        module.main(["--tasks", str(tasks), "--output", str(Path(temp) / "results.json")])
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn("task t1:", problem.getvalue())
                    run_one.assert_not_called()

    def test_claude_answer_is_graded_against_the_required_terms(self):
        full = self.claude_record(json.dumps({"result": "ALPHA then beta", "modelUsage": {"haiku-x": {}}}))
        self.assertEqual((full["exit"], full["required_met"], full["actual_model"]), (0, True, "haiku-x"))
        partial = self.claude_record(json.dumps({"result": "alpha only", "modelUsage": None}))
        self.assertEqual(partial["required_hits"], {"alpha": True, "beta": False})
        self.assertEqual((partial["exit"], partial["required_met"]), (0, False))
        # A task file may lower the threshold from "every term".
        self.assertTrue(self.claude_record(json.dumps({"result": "alpha only"}), min_hits=1)["required_met"])
        self.assertFalse(self.claude_record(json.dumps({"result": "neither"}), min_hits=1)["required_met"])

    def test_exit_status_fails_when_a_run_misses_its_required_hits(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)

            def run_one(repo, task, model, *budget):
                hits, met = module.grade(task, "alpha only" if model == "partial" else "alpha beta")
                return {"task": task["id"], "model": model, "exit": 0, "required_hits": hits,
                        "required_met": met}

            for task, models, expected in (({}, ["full"], 0), ({}, ["full", "partial"], 1),
                                           ({"min_hits": 1}, ["full", "partial"], 0)):
                with self.subTest(name, task=task, models=models), \
                        tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                    tasks = Path(temp) / "tasks.json"
                    tasks.write_text(json.dumps([{"id": "t1", "prompt": "p", "required": ["alpha", "beta"], **task}]),
                                     encoding="utf-8")
                    printed = io.StringIO()
                    with mock.patch.object(module, "run_one", run_one), \
                            mock.patch.object(module, "tree_status", return_value=""), \
                            contextlib.redirect_stdout(printed):
                        code = module.main(["--tasks", str(tasks), "--models", *models, "--jobs", "1",
                                            "--output", str(Path(temp) / "results.json")])
                    self.assertEqual(code, expected)
                    self.assertEqual("below the required hits" in printed.getvalue(), expected == 1)

    def test_a_run_that_changes_the_working_tree_fails_the_read_only_check(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            extra = (.2,) if name == "compare-claude-readonly" else ()
            snapshot = {"head": "c1", "status": " M a.py\n", "diff": "d1", "files": {"notes.txt": "h1"}}
            for statuses, exit_code, expected, problem in (
                    (["", ""], 0, 0, None), ([snapshot, dict(snapshot)], 0, 0, None),
                    # The status line of an already modified file stays the same; its diff does not.
                    ([snapshot, dict(snapshot, diff="d2")], 0, 1, "tracked file content changed during the run"),
                    ([snapshot, dict(snapshot, head="c2")], 0, 1, "HEAD changed during the run"),
                    ([snapshot, dict(snapshot, files={"notes.txt": "h2", "build/out.log": "h3"})], 0, 1,
                     "build/out.log, notes.txt changed during the run"),
                    ([snapshot, dict(snapshot, head="c2", status="")], 0, 1,
                     "HEAD, git status changed during the run"),
                    (["", "?? new.txt\n"], 0, 1, "git status changed during the run"),
                    (["", "?? new.txt\n"], 124, 124, "git status changed during the run"),
                    (["", None], 0, 1, "git status unavailable; the run is unverified as read-only"),
                    ([None, ""], 0, 1, "git status unavailable; the run is unverified as read-only")):
                with self.subTest(name, statuses=statuses, exit_code=exit_code), \
                        mock.patch.object(module, "tree_status", side_effect=statuses), \
                        mock.patch.object(module, "run_one", return_value={"task": "t1", "exit": exit_code}):
                    record = module.run_checked(Path("."), {"id": "t1"}, "m", *extra)
                self.assertEqual(record["exit"], expected)
                self.assertEqual(record.get("read_only_error"), problem)

    def test_a_tasks_file_that_is_not_a_non_empty_list_of_objects_is_refused_before_any_run(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            for content in ({}, [], {"id": "t1", "required": ["a"]}, ["t1"], [{"id": "t1", "required": ["a"]}, 3]):
                with self.subTest(name, content=content), \
                        tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                    tasks = Path(temp) / "tasks.json"
                    tasks.write_text(json.dumps(content), encoding="utf-8")
                    with mock.patch.object(module, "run_one") as run_one, \
                            contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        module.main(["--tasks", str(tasks), "--output", str(Path(temp) / "results.json")])
                    self.assertEqual(error.exception.code, 2)
                    run_one.assert_not_called()

    def test_hitting_the_snapshot_file_bound_is_unverified_not_a_clean_pass(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            extra = (.2,) if name == "compare-claude-readonly" else ()
            snapshot = {"head": "c1", "status": "", "diff": "d", "files": {"a": "h"}, "truncated": True}
            with self.subTest(name), \
                    mock.patch.object(module, "tree_status", side_effect=[snapshot, dict(snapshot)]), \
                    mock.patch.object(module, "run_one", return_value={"task": "t1", "exit": 0}):
                record = module.run_checked(Path("."), {"id": "t1"}, "m", *extra)
            self.assertEqual(record["exit"], 1)
            self.assertIn("unverified as read-only", record["read_only_error"])

    def test_repo_that_is_not_a_git_work_tree_is_refused_before_any_run(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            with self.subTest(name), tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                tasks = Path(temp) / "tasks.json"
                tasks.write_text(json.dumps([{"id": "t1", "prompt": "p", "required": ["alpha"]}]), encoding="utf-8")
                with mock.patch.object(module, "run_one") as run_one, \
                        mock.patch.object(module, "tree_status", return_value=None), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    module.main(["--tasks", str(tasks), "--output", str(Path(temp) / "results.json")])
                self.assertEqual(error.exception.code, 2)
                run_one.assert_not_called()

    def test_tree_status_reads_a_real_work_tree_and_nothing_else(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            git = module.resolve_executable("git", (".exe", ".com") if os.name == "nt" else ("",))
            if git is None:
                self.skipTest("git is not installed")
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                plain, repo = Path(temp) / "plain", Path(temp) / "repo"
                plain.mkdir()
                self.assertIsNone(module.tree_status(plain))
                module.subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
                empty = module.tree_status(repo)
                self.assertEqual((empty["head"], empty["status"], empty["files"]), ("", "", {}))
                (repo / "sub").mkdir()
                (repo / "sub/one.txt").write_text("1")
                before = module.tree_status(repo)
                self.assertEqual(before["status"].strip(), "?? sub/one.txt")
                self.assertEqual(list(before["files"]), ["sub/one.txt"])
                # A second file in an already-untracked folder still changes the snapshot.
                (repo / "sub/two.txt").write_text("2")
                self.assertNotEqual(module.tree_status(repo), before)

    def test_tree_status_sees_writes_inside_a_submodule(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            git = module.resolve_executable("git", (".exe", ".com") if os.name == "nt" else ("",))
            if git is None:
                self.skipTest("git is not installed")

            def run(*args, cwd):
                module.subprocess.run([git, "-c", "protocol.file.allow=always", "-c", "core.autocrlf=false",
                                       "-c", "user.email=a@b.c", "-c", "user.name=a", *args],
                                      cwd=cwd, check=True, capture_output=True)

            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                origin, repo = Path(temp) / "origin", Path(temp) / "repo"
                for folder in (origin, repo):
                    folder.mkdir()
                    run("init", "-q", cwd=folder)
                (origin / "a.txt").write_text("one\n")
                run("add", ".", cwd=origin)
                run("commit", "-qm", "x", cwd=origin)
                run("submodule", "add", "-q", str(origin), "sub", cwd=repo)
                run("commit", "-qm", "add submodule", cwd=repo)
                sub = repo / "sub"
                # Nothing written: the snapshot is stable, with the submodule in it.
                clean = module.tree_status(repo)
                self.assertEqual(clean, module.tree_status(repo))
                self.assertEqual(list(clean["submodules"]), ["sub"])
                self.assertFalse(clean["truncated"])
                # D: a first edit with the submodule's status silenced by ignore=all.
                run("config", "submodule.sub.ignore", "all", cwd=repo)
                silent = module.tree_status(repo)
                (sub / "a.txt").write_text("two\n")
                edited = module.tree_status(repo)
                self.assertNotEqual(edited, silent)
                self.assertIn("submodule work trees", module.tree_changes(silent, edited))
                run("config", "--unset", "submodule.sub.ignore", cwd=repo)
                # C2: a second edit, once the submodule is already dirty.
                dirty = module.tree_status(repo)
                (sub / "a.txt").write_text("three\n")
                self.assertNotEqual(module.tree_status(repo), dirty)
                # C: a new file in the already dirty submodule.
                dirty = module.tree_status(repo)
                (sub / "new.txt").write_text("n")
                self.assertNotEqual(module.tree_status(repo), dirty)

    def test_tree_status_leaves_an_untouched_submodule_read_only(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            git = module.resolve_executable("git", (".exe", ".com") if os.name == "nt" else ("",))
            if git is None:
                self.skipTest("git is not installed")

            def run(*args, cwd):
                module.subprocess.run([git, "-c", "protocol.file.allow=always", "-c", "user.email=a@b.c",
                                       "-c", "user.name=a", *args], cwd=cwd, check=True, capture_output=True)

            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                origin, repo = Path(temp) / "origin", Path(temp) / "repo"
                for folder in (origin, repo):
                    folder.mkdir()
                    run("init", "-q", cwd=folder)
                (origin / "a.txt").write_text("one\n")
                run("add", ".", cwd=origin)
                run("commit", "-qm", "x", cwd=origin)
                run("submodule", "add", "-q", str(origin), "sub", cwd=repo)
                run("commit", "-qm", "add submodule", cwd=repo)
                before = module.tree_status(repo)
                (repo / "outside.txt").write_text("x")  # a change outside it still shows
                after = module.tree_status(repo)
                self.assertEqual(before["submodules"], after["submodules"])
                self.assertNotEqual(before, after)
                (repo / "outside.txt").unlink()
                self.assertEqual(module.tree_status(repo), before)

    def test_tree_status_sees_what_a_plain_git_status_misses(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            git = module.resolve_executable("git", (".exe", ".com") if os.name == "nt" else ("",))
            if git is None:
                self.skipTest("git is not installed")
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                repo = Path(temp) / "repo"

                def run(*args):
                    module.subprocess.run([git, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                                          check=True, capture_output=True)

                module.subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
                (repo / ".gitignore").write_text("build/\n*.log\n")
                (repo / "a.py").write_text("one\n")
                (repo / "build").mkdir()
                (repo / "build/out.bin").write_bytes(b"\x00\x01")
                (repo / "notes.txt").write_text("untracked\n")
                run("add", ".gitignore", "a.py")
                run("commit", "-q", "-m", "first")
                (repo / "a.py").write_text("one\ntwo\n")
                before = module.tree_status(repo)
                self.assertEqual(before, module.tree_status(repo))  # reading it changes nothing
                self.assertEqual(sorted(before["files"]), ["build/out.bin", "notes.txt"])

                # An edit to a file that was already modified: the status line is unchanged.
                (repo / "a.py").write_text("one\ntwo\nthree\n")
                edited = module.tree_status(repo)
                self.assertEqual(edited["status"], before["status"])
                self.assertEqual(module.tree_changes(before, edited), "tracked file content")

                # A write under an ignored path, new or over an existing file of the same size.
                (repo / "build/new.log").write_text("log")
                written = module.tree_status(repo)
                self.assertEqual(module.tree_changes(edited, written), "git status, build/new.log")
                (repo / "build/out.bin").write_bytes(b"\x00\x02")
                self.assertEqual(module.tree_changes(written, module.tree_status(repo)), "build/out.bin")
                (repo / "notes.txt").write_text("UNTRACKED\n")
                self.assertIn("notes.txt", module.tree_changes(written, module.tree_status(repo)))

                # A file created and committed during the run leaves the status clean: HEAD moved.
                staged = module.tree_status(repo)
                (repo / "c.py").write_text("new\n")
                run("add", "c.py")
                run("commit", "-q", "-m", "second", "--", "c.py")
                committed = module.tree_status(repo)
                self.assertEqual(committed["status"], staged["status"])
                self.assertEqual(module.tree_changes(staged, committed), "HEAD, git refs")

                # Past the file bound the snapshot stops fingerprinting instead of growing.
                with mock.patch.object(module, "SNAPSHOT_FILES", 1):
                    capped = module.tree_status(repo)
                    self.assertEqual(len(capped["files"]), 1)
                    self.assertTrue(capped["truncated"])
                self.assertFalse(module.tree_status(repo)["truncated"])
                with mock.patch.object(module, "SNAPSHOT_HASH_BYTES", 0):
                    self.assertRegex(module.tree_status(repo)["files"]["notes.txt"], r"^\d+:\d+$")

    def test_a_write_to_the_repository_itself_is_not_read_only(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            git = module.resolve_executable("git", (".exe", ".com") if os.name == "nt" else ("",))
            if git is None:
                self.skipTest("git is not installed")
            extra = (.2,) if name == "compare-claude-readonly" else ()
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                repo = Path(temp) / "repo"

                def run(*args):
                    module.subprocess.run([git, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                                          check=True, capture_output=True)

                def hook():
                    (repo / ".git/hooks").mkdir(exist_ok=True)
                    (repo / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")

                module.subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
                (repo / "a.py").write_text("one\n")
                run("add", "a.py")
                run("commit", "-q", "-m", "first")
                run("branch", "other")

                # Each write leaves HEAD's commit, the status, the diff and every loose file as they were.
                for label, write in (("git refs", lambda: run("tag", "probe")),
                                     ("git refs", lambda: run("symbolic-ref", "HEAD", "refs/heads/other")),
                                     ("git refs", lambda: run("update-ref", "refs/stash", "HEAD")),
                                     ("git config", lambda: run("config", "probe.key", "1")),
                                     ("git hooks", hook),
                                     ("git hooks", lambda: (repo / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 1\n"))):
                    before = module.tree_status(repo)
                    self.assertEqual(before, module.tree_status(repo))  # reading it changes nothing
                    write()
                    after = module.tree_status(repo)
                    for key in ("head", "status", "diff", "files", "truncated"):
                        self.assertEqual(after[key], before[key], (label, key))
                    self.assertIn(label, module.tree_changes(before, after))
                    with mock.patch.object(module, "tree_status", side_effect=[before, after]), \
                            mock.patch.object(module, "run_one", return_value={"task": "t1", "exit": 0}):
                        record = module.run_checked(repo, {"id": "t1"}, "m", *extra)
                    self.assertEqual(record["exit"], 1, label)
                    self.assertIn(f"{label} changed during the run", record["read_only_error"])

                # A stash entry moves the stash list.
                before = module.tree_status(repo)
                (repo / "a.py").write_text("two\n")
                run("stash")
                self.assertNotEqual(module.tree_status(repo)["stash"], before["stash"])

                # More hooks than the bound: the snapshot says it did not compare them all.
                (repo / ".git/hooks/pre-push").write_text("#!/bin/sh\nexit 0\n")
                with mock.patch.object(module, "GIT_STATE_FILES", 1):
                    self.assertTrue(module.tree_status(repo)["truncated"])
                self.assertFalse(module.tree_status(repo)["truncated"])

    def test_writes_under_volatile_ignored_paths_pass_and_other_ignored_writes_fail(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            git = module.resolve_executable("git", (".exe", ".com") if os.name == "nt" else ("",))
            if git is None:
                self.skipTest("git is not installed")
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                repo = Path(temp) / "repo"
                module.subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
                (repo / ".gitignore").write_text("build/\n__pycache__/\n.hunch/\n")
                (repo / "build").mkdir()
                (repo / "build/out.bin").write_bytes(b"1")
                before = module.tree_status(repo)

                # Concurrent sessions and hooks write here; the read-only check ignores it.
                (repo / "pkg/__pycache__").mkdir(parents=True)
                (repo / "pkg/__pycache__/mod.cpython-312.pyc").write_bytes(b"pyc")
                (repo / ".hunch").mkdir()
                (repo / ".hunch/ledger.db").write_bytes(b"db")
                self.assertEqual(module.tree_status(repo), before)

                # Any other ignored write still fails.
                (repo / "build/new.bin").write_bytes(b"2")
                self.assertIn("build/new.bin", module.tree_changes(before, module.tree_status(repo)))

                # The list only covers ignored paths: untracked ones of the same name still count.
                for volatile in ("relay/handoffs/h.md", ".hunch-cache/x", ".test-tmp/a/b", ".playwright-mcp/s.png"):
                    self.assertTrue(module.is_volatile(volatile), volatile)
                self.assertTrue(module.is_volatile("relay/state/x.json"))
                self.assertTrue(module.is_volatile("relay/jev-log.jsonl"))
                self.assertFalse(module.is_volatile("lib/relay/state/x.json"))
                self.assertFalse(module.is_volatile("__pycache__"))
                status = b"?? pkg/__pycache__/a.pyc\0!! pkg/__pycache__/b.pyc\0!! build/c.bin\0"
                self.assertEqual(module.drop_volatile(status), b"?? pkg/__pycache__/a.pyc\0!! build/c.bin\0")

    def test_writes_git_status_cannot_show_are_not_read_only(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            git = module.resolve_executable("git", (".exe", ".com") if os.name == "nt" else ("",))
            if git is None:
                self.skipTest("git is not installed")
            extra = (.2,) if name == "compare-claude-readonly" else ()
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                repo = Path(temp) / "repo"

                def run(*args):
                    module.subprocess.run([git, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                                          check=True, capture_output=True)

                last = []

                def changed(write, label, exact=False):
                    """A write that leaves the status, HEAD and diff alone is still reported, and fails the run."""
                    before = last[-1] if last else module.tree_status(repo)
                    write()
                    after = module.tree_status(repo)
                    last[:] = [after]
                    changes = module.tree_changes(before, after)
                    if exact:
                        self.assertEqual(changes, label)
                    else:
                        self.assertIn(label, changes)
                    with mock.patch.object(module, "tree_status", side_effect=[before, after]), \
                            mock.patch.object(module, "run_one", return_value={"task": "t1", "exit": 0}):
                        record = module.run_checked(repo, {"id": "t1"}, "m", *extra)
                    self.assertEqual(record["exit"], 1, label)
                    self.assertIn("changed during the run", record["read_only_error"], label)

                module.subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
                for tracked in ("skip.py", "assume.py", "plain.py", "third.py"):
                    (repo / tracked).write_text("one\n")
                (repo / ".gitignore").write_text("__pycache__/\n")
                run("add", ".")
                run("commit", "-q", "-m", "first")

                # Edits git status and diff never show: skip-worktree, assume-unchanged.
                run("update-index", "--skip-worktree", "skip.py")
                run("update-index", "--assume-unchanged", "assume.py")
                self.assertEqual(module.tree_status(repo)["status"], "")
                last.clear()
                changed(lambda: (repo / "skip.py").write_text("edited\n"), "skip-worktree or assume-unchanged file content")
                changed(lambda: (repo / "assume.py").write_text("edited\n"), "skip-worktree or assume-unchanged file content")
                # The run sets the flag itself and then edits the file (flag and content both move).
                changed(lambda: (run("update-index", "--skip-worktree", "plain.py"),
                                 (repo / "plain.py").write_text("edited\n")),
                        "skip-worktree or assume-unchanged flags")
                changed(lambda: run("update-index", "--no-skip-worktree", "plain.py"),
                        "skip-worktree or assume-unchanged flags")
                (repo / "plain.py").write_text("one\n")
                last.clear()

                # More flagged files than the bound: the snapshot says it did not hash them all.
                run("update-index", "--assume-unchanged", "third.py")
                with mock.patch.object(module, "GIT_STATE_FILES", 1):
                    flagged = module.tree_status(repo)
                    self.assertTrue(flagged["truncated"])
                    with mock.patch.object(module, "tree_status", side_effect=[flagged, dict(flagged)]), \
                            mock.patch.object(module, "run_one", return_value={"task": "t1", "exit": 0}):
                        record = module.run_checked(repo, {"id": "t1"}, "m", *extra)
                    self.assertEqual(record["exit"], 1)
                    self.assertIn("unverified as read-only", record["read_only_error"])
                self.assertFalse(module.tree_status(repo)["truncated"])

                # The repository's own files: excludes, attributes, the marks of a merge or rebase.
                (repo / ".git/info").mkdir(exist_ok=True)
                changed(lambda: (repo / ".git/info/exclude").write_text("*.tmp\n"), "git state files")
                changed(lambda: (repo / ".git/info/attributes").write_text("*.bin binary\n"), "git state files")
                changed(lambda: [(repo / ".git" / mark).write_text("0" * 40 + "\n")
                                 for mark in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD")], "git state files")
                changed(lambda: (repo / ".git/rebase-merge").mkdir(), "git state files")
                changed(lambda: (repo / ".git/rebase-merge/head-name").write_text("refs/heads/x\n"), "git state files")
                changed(lambda: (repo / ".git/rebase-apply").mkdir(), "git state files")

                # A new work tree; a new empty folder (git status never lists one).
                changed(lambda: run("worktree", "add", "--detach", str(Path(temp) / "second")), "git worktrees")
                changed(lambda: (repo / "emptydir").mkdir(), "empty folders", exact=True)
                changed(lambda: (repo / "emptydir/nested").mkdir(), "empty folders", exact=True)

                # A symbolic-ref change moves the refs and nothing else.
                run("branch", "other")
                last.clear()
                changed(lambda: run("symbolic-ref", "HEAD", "refs/heads/other"), "git refs", exact=True)

                # A new folder holding only ignored volatile files is not an empty folder.
                before = last[-1]
                (repo / "pkg/__pycache__").mkdir(parents=True)
                (repo / "pkg/__pycache__/m.cpython-312.pyc").write_bytes(b"pyc")
                after = module.tree_status(repo)
                self.assertEqual(after["dirs"], before["dirs"])
                self.assertEqual(after, before)

                # More empty folders than the walk bound: unverified, not a clean pass.
                with mock.patch.object(module, "SNAPSHOT_FILES", 1):
                    self.assertTrue(module.tree_status(repo)["truncated"])

    def test_a_missing_or_non_json_tasks_file_is_a_usage_error(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            # 200000 open brackets nest past the JSON decoder's recursion limit.
            for content in (None, "not json", "[{", "[" * 200000):
                with self.subTest(name, content=content), \
                        tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                    tasks = Path(temp) / "tasks.json"
                    if content is not None:
                        tasks.write_text(content, encoding="utf-8")
                    problem = io.StringIO()
                    with mock.patch.object(module, "run_one") as run_one, \
                            contextlib.redirect_stderr(problem), self.assertRaises(SystemExit) as error:
                        module.main(["--tasks", str(tasks), "--output", str(Path(temp) / "results.json")])
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn("tasks file cannot be read as JSON", problem.getvalue())
                    self.assertNotIn("Traceback", problem.getvalue())
                    run_one.assert_not_called()

    def test_a_task_without_a_string_id_and_prompt_is_refused_before_any_run(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            for task in ({"prompt": "p"}, {"id": "", "prompt": "p"}, {"id": 3, "prompt": "p"},
                         {"id": "t1"}, {"id": "t1", "prompt": " "}, {"id": "t1", "prompt": ["p"]}):
                with self.subTest(name, task=task), \
                        tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                    tasks = Path(temp) / "tasks.json"
                    tasks.write_text(json.dumps([{"required": ["alpha"], **task}]), encoding="utf-8")
                    problem = io.StringIO()
                    with mock.patch.object(module.subprocess, "Popen") as popen, \
                            mock.patch.object(module.subprocess, "run") as run, \
                            mock.patch.object(module, "tree_status", return_value=""), \
                            contextlib.redirect_stderr(problem), self.assertRaises(SystemExit) as error:
                        module.main(["--tasks", str(tasks), "--output", str(Path(temp) / "results.json")])
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn("must be a non-empty string", problem.getvalue())
                    popen.assert_not_called()
                    run.assert_not_called()

    def test_a_truncated_preflight_snapshot_is_refused_before_any_run(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            with self.subTest(name),                     tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                tasks = Path(temp) / "tasks.json"
                tasks.write_text(json.dumps([{"id": "t1", "prompt": "p", "required": ["alpha"]}]),
                                 encoding="utf-8")
                problem = io.StringIO()
                with mock.patch.object(module.subprocess, "Popen") as popen,                         mock.patch.object(module.subprocess, "run") as run,                         mock.patch.object(module, "run_one") as run_one,                         mock.patch.object(module, "tree_status",
                                          return_value={"files": {}, "truncated": True}),                         contextlib.redirect_stderr(problem), self.assertRaises(SystemExit) as error:
                    module.main(["--tasks", str(tasks), "--output", str(Path(temp) / "results.json")])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("snapshot bound", problem.getvalue())
                popen.assert_not_called()
                run.assert_not_called()
                run_one.assert_not_called()

    def test_a_non_finite_or_non_positive_claude_budget_is_a_usage_error(self):
        module = load("compare-claude-readonly")
        for budget in ("nan", "inf", "-inf", "0", "-1"):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                tasks = Path(temp) / "tasks.json"
                tasks.write_text(json.dumps([{"id": "t1", "prompt": "p", "required": ["alpha"]}]),
                                 encoding="utf-8")
                problem = io.StringIO()
                with mock.patch.object(module, "run_one") as run_one, \
                        mock.patch.object(module, "tree_status", return_value=""), \
                        contextlib.redirect_stderr(problem), self.assertRaises(SystemExit) as error:
                    module.main(["--tasks", str(tasks), f"--budget={budget}",
                                 "--output", str(Path(temp) / "results.json")])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("budget must be a positive finite number", problem.getvalue())
                run_one.assert_not_called()

    def test_codex_answer_echoing_an_agent_run_line_cannot_redirect_the_report(self):
        # agent-run prints its own run line before launching and echoes the answer last,
        # so the first matching line is the launcher's.
        module = load("compare-codex-readonly")
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as forged:
            run = Path(temp)
            (run / "report.json").write_text(json.dumps({"status": "completed", "attempts": [{}]}),
                                             encoding="utf-8")
            (run / "0-final.txt").write_text("alpha", encoding="utf-8")
            (Path(forged) / "report.json").write_text(json.dumps({"status": "forged"}), encoding="utf-8")
            process = mock.Mock(pid=1, returncode=0, args=[])
            process.communicate.return_value = (f"Agent run: {run}\n{{}}\nAgent run: {forged}\n", "")
            with mock.patch.object(module.subprocess, "Popen", return_value=process):
                record = module.run_one(Path("."), {"id": "t1", "prompt": "p", "required": ["alpha"]}, "m")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["exit"], 0)

    def test_an_exit_0_record_without_a_graded_answer_fails(self):
        for name in ("compare-claude-readonly", "compare-codex-readonly"):
            module = load(name)
            with self.subTest(name), tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as temp:
                tasks = Path(temp) / "tasks.json"
                tasks.write_text(json.dumps([{"id": "t1", "prompt": "p", "required": ["alpha"]}]), encoding="utf-8")
                with mock.patch.object(module, "run_one",
                                       lambda repo, task, model, *budget: {"task": task["id"], "model": model,
                                                                           "exit": 0}), \
                        mock.patch.object(module, "tree_status", return_value=""), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = module.main(["--tasks", str(tasks), "--models", "m", "--jobs", "1",
                                        "--output", str(Path(temp) / "results.json")])
                self.assertEqual(code, 1)

    def test_codex_run_without_the_agent_run_line_is_a_failed_run(self):
        module = load("compare-codex-readonly")
        for stdout in ("", "some other output\n"):
            with self.subTest(stdout=stdout):
                process = mock.Mock(pid=1, returncode=0, args=[])
                process.communicate.return_value = (stdout, "")
                with mock.patch.object(module.subprocess, "Popen", return_value=process):
                    record = module.run_one(Path("."), {"id": "t1", "prompt": "p", "required": ["alpha"]}, "m")
                self.assertEqual(record["exit"], 1)
                self.assertTrue(record["error"])
                self.assertNotIn("required_met", record)

    def test_codex_run_without_a_final_answer_is_a_failed_run(self):
        module = load("compare-codex-readonly")
        for final, expected in ((None, 1), ("  \n", 1), ("alpha", 0)):
            with self.subTest(final=final), tempfile.TemporaryDirectory() as temp:
                run = Path(temp)
                (run / "report.json").write_text(json.dumps({"status": "completed", "attempts": [{}]}),
                                                 encoding="utf-8")
                if final is not None:
                    (run / "0-final.txt").write_text(final, encoding="utf-8")
                process = mock.Mock(pid=1, returncode=0, args=[])
                process.communicate.return_value = (f"Agent run: {run}\n", "")
                with mock.patch.object(module.subprocess, "Popen", return_value=process):
                    record = module.run_one(Path("."), {"id": "t1", "prompt": "p", "required": ["alpha"]}, "m")
                self.assertEqual(record["exit"], expected)
                self.assertEqual(record.get("error"), "no final answer" if expected else None)
                self.assertEqual(record.get("required_met"), None if final is None else final == "alpha")

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
