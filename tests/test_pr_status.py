import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "bin/pr-status"
LOADER = importlib.machinery.SourceFileLoader("pr_status", str(PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
pr_status = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(pr_status)


def pull_request(number):
    return {"number": number, "title": f"Change {number}", "headRefName": f"branch-{number}"}


class PrStatusTests(unittest.TestCase):
    def run_status(self, argv, response):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(pr_status, "gh", return_value=json.dumps(response)) as gh:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                pr_status.main(argv)
        return output.getvalue(), errors.getvalue(), gh.call_args.args

    def test_listing_warns_only_when_results_are_truncated(self):
        for count in (0, 99, 100, 101):
            with self.subTest(count=count):
                output, errors, args = self.run_status([], [pull_request(n) for n in range(1, count + 1)])
                self.assertEqual(args[args.index("--limit") + 1], "101")
                self.assertEqual("more exist" in errors, count > 100)
                rows = [line for line in output.splitlines() if line.startswith("#")]
                self.assertEqual(len(rows), min(count, 100))

    def test_custom_limit_and_filters_are_forwarded(self):
        output, errors, args = self.run_status(
            ["--limit", "2", "-R", "owner/repo", "--author", "someone"],
            [pull_request(3), pull_request(2), pull_request(1)],
        )
        self.assertEqual(args[args.index("--limit") + 1], "3")
        self.assertEqual(args[args.index("-R") + 1], "owner/repo")
        self.assertEqual(args[args.index("--author") + 1], "someone")
        self.assertIn("showing 2", errors)
        self.assertNotIn("Change 1", output)

    def test_explicit_numbers_run_concurrently_with_a_four_request_bound(self):
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active, peak = 0, 0
        calls = []

        def gh(*args):
            nonlocal active, peak
            self.assertEqual(args[:2], ("pr", "view"))
            self.assertEqual(args[args.index("-R") + 1], "owner/repo")
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append(int(args[2]))
            try:
                # Two waves must each overlap, without allowing a fifth worker.
                barrier.wait(timeout=10)
                return json.dumps(pull_request(int(args[2])))
            finally:
                with lock:
                    active -= 1

        output = io.StringIO()
        with patch.object(pr_status, "gh", side_effect=gh), contextlib.redirect_stdout(output):
            pr_status.main(["8", "7", "6", "5", "4", "3", "2", "#1", "1", "-R", "owner/repo"])
        self.assertEqual(peak, 4)
        self.assertEqual(sorted(calls), list(range(1, 9)))
        rows = [int(line.split()[1]) for line in output.getvalue().splitlines() if line.startswith("#")]
        self.assertEqual(rows, list(range(1, 9)))

    def test_fetch_error_does_not_print_a_partial_success_table(self):
        output = io.StringIO()
        with patch.object(pr_status, "gh", side_effect=SystemExit("request failed")):
            with contextlib.redirect_stdout(output), self.assertRaisesRegex(SystemExit, "request failed"):
                pr_status.main(["1", "2"])
        self.assertEqual(output.getvalue(), "")

    def test_options_can_appear_between_pr_numbers(self):
        calls = []

        def gh(*args):
            calls.append(args)
            return json.dumps(pull_request(int(args[2])))

        with patch.object(pr_status, "gh", side_effect=gh), contextlib.redirect_stdout(io.StringIO()):
            pr_status.main(["1", "--repo", "owner/repo", "#2", "--failed"])
        self.assertEqual(sorted(int(args[2]) for args in calls), [1, 2])
        self.assertTrue(all(args[args.index("-R") + 1] == "owner/repo" for args in calls))

    def test_title_outside_the_console_code_page_prints_as_utf8(self):
        # A piped stdout on Windows is a code-page wrapper; "→" is not in cp1252.
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
        prs = [dict(pull_request(1), title="fix → arrow"), pull_request(2)]
        with patch.object(pr_status, "gh", return_value=json.dumps(prs)), \
             patch.object(pr_status.sys, "stdout", stream), contextlib.redirect_stderr(io.StringIO()):
            pr_status.main([])
            stream.flush()
        rows = raw.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[1].endswith("fix → arrow"))

    def test_failed_lists_failures_before_waits_so_the_cut_never_hides_one(self):
        rollup = [{"name": f"pending-{i}", "status": "IN_PROGRESS"} for i in range(9)]
        rollup.append({"name": "broken", "conclusion": "FAILURE"})
        pr = dict(pull_request(1), statusCheckRollup=rollup)
        output, _, _ = self.run_status(["--failed"], [pr])
        listed = [row.strip() for row in output.splitlines()[2:]]
        self.assertEqual(len(listed), 8)
        self.assertEqual(listed[0], "FAIL broken")
        self.assertEqual(listed[1:], [f"wait pending-{i}" for i in range(7)])

    def test_empty_or_non_json_gh_output_is_a_one_line_error(self):
        for text in ("", "not json"):
            for argv in ([], ["5"]):
                with self.subTest(text=text, argv=argv), patch.object(pr_status, "gh", return_value=text):
                    errors = io.StringIO()
                    with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                        pr_status.main(argv)
                    self.assertEqual(caught.exception.code, 1)
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertNotIn("Traceback", errors.getvalue())

    def test_several_bad_replies_make_exactly_one_error_line(self):
        for text in ("", "not json", json.dumps(5), json.dumps({"number": "x"})):
            with self.subTest(text=text), patch.object(pr_status, "gh", return_value=text):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                        self.assertRaises(SystemExit) as caught:
                    pr_status.main(["1", "2", "3"])
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertNotIn("Traceback", errors.getvalue())

    def test_a_null_title_or_branch_prints_as_empty(self):
        output, _, _ = self.run_status([], [{"number": 3, "title": None, "headRefName": None}])
        self.assertIn("#   3", output)

    def test_a_bad_record_prints_no_partial_table(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(pr_status, "gh", return_value=json.dumps([pull_request(1), {"title": "no number"}])):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                    self.assertRaises(SystemExit) as caught:
                pr_status.main([])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_json_of_the_wrong_shape_is_a_one_line_error(self):
        cases = [
            ([], None), ([], 5), ([], "x"), ([], {"number": 1}), ([], [None]), ([], [5]),
            ([], [{"title": "no number"}]), ([], [{"number": "7"}]), ([], [{"number": True}]),
            ([], [{"number": None}]), ([], [dict(pull_request(5), statusCheckRollup="x")]),
            (["5"], None), (["5"], []), (["5"], 5), (["5"], [pull_request(5)]),
            (["5"], {"title": "no number"}), (["5"], {"number": 5.5}),
            (["5"], dict(pull_request(5), statusCheckRollup={"name": "ci"})),
        ]
        for argv, response in cases:
            with self.subTest(argv=argv, response=response), \
                    patch.object(pr_status, "gh", return_value=json.dumps(response)):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                        self.assertRaises(SystemExit) as caught:
                    pr_status.main(argv)
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("unexpected shape", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())

    def test_fields_of_another_type_print_as_text(self):
        record = {"number": 7, "title": 123, "headRefName": 45, "mergeStateStatus": 1, "reviewDecision": 2,
                  "state": 3, "statusCheckRollup": [None, 5, "x", ["y"], {"name": 9, "conclusion": 1},
                                                    {"name": ["ci"], "workflowName": {"a": 1}, "detailsUrl": 4,
                                                     "conclusion": "SUCCESS"}]}
        for argv, response in ((["--failed"], [record]), (["7", "--failed"], record)):
            with self.subTest(argv=argv):
                output, errors, _ = self.run_status(argv, response)
                self.assertEqual(errors, "")
                row = next(line for line in output.splitlines() if line.startswith("#"))
                self.assertIn("#   7 wait 1ok/0bad/1wait", row)
                self.assertTrue(row.endswith(f"{'45':24} 123"), row)
                self.assertIn("wait 9", output)
        # A rollup of nothing but non-objects is never green and never "none".
        output, errors, _ = self.run_status([], [dict(pull_request(1), statusCheckRollup=[None])])
        self.assertEqual(errors, "")
        self.assertIn("#   1 unsure 0ok/0bad/0wait/1?", output)

    def test_explicit_number_shows_a_merged_or_closed_pr_as_such(self):
        def gh(*args):
            self.assertIn("state", args[args.index("--json") + 1].split(","))
            state = {"1": "MERGED", "2": "CLOSED", "3": "OPEN"}[args[2]]
            return json.dumps(dict(pull_request(int(args[2])), state=state, mergeStateStatus="CLEAN",
                                   isDraft=args[2] == "2"))

        output = io.StringIO()
        with patch.object(pr_status, "gh", side_effect=gh), contextlib.redirect_stdout(output):
            pr_status.main(["1", "2", "3"])
        # "#   1 none  merged ...": the merge column follows the number and the CI verdict.
        merge = [line.split()[3] for line in output.getvalue().splitlines() if line.startswith("#")]
        self.assertEqual(merge, ["merged", "closed", "clean"])

    def test_invalid_arguments_do_not_query_github(self):
        for argv in (["--limit", "0"], ["--limit", "-1"], ["--limit", "abc"], ["--typo"], ["-R"]):
            with self.subTest(argv=argv), patch.object(pr_status, "gh") as gh:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    pr_status.main(argv)
                self.assertEqual(error.exception.code, 2)
                gh.assert_not_called()


class GhLaunchTests(unittest.TestCase):
    def launch(self, platform, wrapper, pathext=".COM;.EXE;.BAT;.CMD", result=None, error=None,
               gh_path="C:/tools/gh.exe"):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if error is not None:
                raise error
            return result or subprocess.CompletedProcess(command, 0, "[]", "")

        with patch.object(pr_status.sys, "platform", platform), \
             patch.object(pr_status.os.path, "expanduser", return_value=wrapper), \
             patch.object(pr_status.os, "access", return_value=True), \
             patch.dict(pr_status.os.environ, {"PATHEXT": pathext}), \
             patch.object(pr_status, "resolve_executable", return_value=gh_path), \
             patch.object(pr_status.subprocess, "run", side_effect=run):
            output = pr_status.gh("pr", "list", "--json", "number")
        return output, calls

    @unittest.skipUnless(os.name == "nt", "shutil.which applies PATHEXT only on Windows")
    def test_windows_resolves_the_wrapper_only_for_exe_or_com(self):
        with tempfile.TemporaryDirectory() as temp:
            wrapper_dir = Path(temp) / ".hunch"
            wrapper_dir.mkdir()
            base = str(wrapper_dir / "agent-gh")
            gh_path = "C:/tools/gh.exe"
            with patch.object(pr_status.sys, "platform", "win32"), \
                 patch.object(pr_status.os.path, "expanduser", return_value=base), \
                 patch.object(pr_status, "resolve_executable", return_value=gh_path) as resolve:
                self.assertEqual(pr_status.wrapper_command(), gh_path)  # nothing present yet
                resolve.assert_called_with("gh", (".com", ".exe"))

                (wrapper_dir / "agent-gh.cmd").write_bytes(b"")
                self.assertEqual(pr_status.wrapper_command(), gh_path)  # .cmd runs through cmd.exe

                (wrapper_dir / "agent-gh").write_bytes(b"")
                self.assertEqual(pr_status.wrapper_command(), gh_path)  # extensionless: no PATHEXT match

                exe = wrapper_dir / "agent-gh.exe"
                exe.write_bytes(b"")
                # shutil.which reconstructs the suffix from PATHEXT casing (.EXE).
                self.assertEqual(pr_status.wrapper_command().lower(), str(exe).lower())

                exe.unlink()
                com = wrapper_dir / "agent-gh.com"
                com.write_bytes(b"")
                self.assertEqual(pr_status.wrapper_command().lower(), str(com).lower())

    def test_posix_uses_executable_wrapper(self):
        output, calls = self.launch("linux", "/home/u/.hunch/agent-gh")
        self.assertEqual(output, "[]")
        self.assertEqual(calls[0][0], ["/home/u/.hunch/agent-gh", "pr", "list", "--json", "number"])
        self.assertEqual(calls[0][1]["errors"], "replace")

    def test_posix_falls_back_to_gh_resolved_from_path(self):
        with patch.object(pr_status.os, "access", return_value=False), \
             patch.object(pr_status.sys, "platform", "linux"), \
             patch.object(pr_status, "resolve_executable", return_value="/usr/bin/gh") as resolve:
            self.assertEqual(pr_status.wrapper_command(), "/usr/bin/gh")
        resolve.assert_called_once_with("gh", ("",))

    def test_launch_errors_exit_with_a_message(self):
        for error in (OSError(193, "not a valid Win32 application"), subprocess.TimeoutExpired("gh", 60)):
            with self.subTest(error=error), self.assertRaises(SystemExit) as caught:
                self.launch("win32", "C:/home/.hunch/agent-gh", error=error)
            self.assertTrue(str(caught.exception.code).startswith(
                "pr-status: pr list --json could not run C:/tools/gh.exe"))

    def test_missing_gh_exits_without_launching(self):
        with self.assertRaises(SystemExit) as caught:
            self.launch("win32", "C:/home/.hunch/agent-gh", gh_path=None,
                        error=AssertionError("must not launch"))
        self.assertEqual(caught.exception.code, "pr-status: pr list --json could not run gh: "
                         "not found on PATH (the current directory is never searched)")

    def test_failed_command_exits_with_its_stderr(self):
        failed = subprocess.CompletedProcess([], 1, "", "HTTP 401\n")
        with self.assertRaises(SystemExit) as caught:
            self.launch("linux", "/home/u/.hunch/agent-gh", result=failed)
        self.assertEqual(caught.exception.code, "pr-status: pr list --json failed: HTTP 401")


class ChecksTests(unittest.TestCase):
    def test_stale_is_a_finished_failure_not_a_wait(self):
        verdict, names = pr_status.checks([{"name": "ci", "conclusion": "STALE"},
                                           {"name": "lint", "conclusion": "SUCCESS"}])
        self.assertEqual(verdict, "FAIL 1ok/1bad/0wait")
        self.assertEqual(names, ["FAIL ci"])

    def test_completed_check_without_a_conclusion_is_neither_a_wait_nor_a_pass(self):
        silent = {"name": "ci", "status": "COMPLETED", "conclusion": ""}
        verdict, names = pr_status.checks([silent, {"name": "lint", "status": "IN_PROGRESS", "conclusion": ""}])
        self.assertEqual(verdict, "wait 0ok/0bad/1wait/1?")
        self.assertEqual(names, ["done ci (no conclusion)", "wait lint"])
        # With nothing left to wait for the summary is still not green.
        verdict, names = pr_status.checks([silent, {"name": "lint", "conclusion": "SUCCESS"}])
        self.assertEqual(verdict, "unsure 1ok/0bad/0wait/1?")
        self.assertEqual(names, ["done ci (no conclusion)"])
        self.assertEqual(pr_status.checks([silent])[0], "unsure 0ok/0bad/0wait/1?")
        # A failure still wins, and a conclusion still decides.
        self.assertEqual(pr_status.checks([silent, {"name": "e2e", "conclusion": "FAILURE"}])[0],
                         "FAIL 0ok/1bad/0wait/1?")
        self.assertEqual(pr_status.checks([{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}])[0],
                         "green 1ok/0bad/0wait")

    def test_a_non_object_entry_is_never_green(self):
        for junk in (None, 5, "x", ["y"], True):
            with self.subTest(junk=junk):
                self.assertEqual(pr_status.checks([junk])[0], "unsure 0ok/0bad/0wait/1?")
                verdict, names = pr_status.checks([{"name": "ci", "conclusion": "SUCCESS"}, junk])
                self.assertEqual(verdict, "unsure 1ok/0bad/0wait/1?")
                self.assertEqual(names, ["done ? (not a check object)"])
        self.assertTrue(pr_status.checks([{"name": "ci", "conclusion": "FAILURE"}, None])[0].startswith("FAIL"))

    def test_a_success_conclusion_on_an_unfinished_check_is_a_wait(self):
        for status in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"):
            with self.subTest(status=status):
                verdict, names = pr_status.checks([{"name": "ci", "status": status, "conclusion": "SUCCESS"}])
                self.assertEqual(verdict, "wait 0ok/0bad/1wait")
                self.assertEqual(names, ["wait ci"])
        # A failure conclusion still wins over an unfinished status.
        verdict, _ = pr_status.checks([{"name": "ci", "status": "IN_PROGRESS", "conclusion": "FAILURE"}])
        self.assertEqual(verdict, "FAIL 0ok/1bad/0wait")

    def test_a_sibling_job_of_one_name_in_one_run_never_hides_a_failure(self):
        # The run id cannot tell a re-run from a sibling job; GitHub's rollup already
        # lists only the latest attempt after a re-run.
        failed = {"__typename": "CheckRun", "name": "test", "workflowName": "CI", "conclusion": "FAILURE",
                  "detailsUrl": "https://github.com/o/r/actions/runs/1/job/10",
                  "startedAt": "2026-01-01T00:00:00Z", "completedAt": "2026-01-01T00:05:00Z"}
        passed = dict(failed, conclusion="SUCCESS", detailsUrl="https://github.com/o/r/actions/runs/1/job/11",
                      startedAt="2026-01-01T00:05:01Z", completedAt="2026-01-01T00:06:00Z")
        # A needs: job skipped after its dependency failed.
        skipped = dict(failed, conclusion="SKIPPED", detailsUrl="https://github.com/o/r/actions/runs/1/job/11",
                       startedAt="2026-01-01T00:05:00Z", completedAt="2026-01-01T00:05:00Z")
        for sibling in (passed, skipped):
            for rollup in ([failed, sibling], [sibling, failed]):
                with self.subTest(sibling=sibling["conclusion"], first=rollup[0]["conclusion"]):
                    self.assertEqual(pr_status.checks(rollup), ("FAIL 1ok/1bad/0wait", ["FAIL test"]))

    def test_exact_duplicates_count_once_as_their_worst_state(self):
        run = {"__typename": "CheckRun", "name": "ci", "workflowName": "CI", "conclusion": "SUCCESS",
               "detailsUrl": "https://github.com/o/r/actions/runs/7/job/1"}
        self.assertEqual(pr_status.checks([run, dict(run)]), ("green 1ok/0bad/0wait", []))
        waiting = dict(run, conclusion="", status="IN_PROGRESS")
        failed = dict(run, conclusion="FAILURE")
        for rollup in ([run, waiting], [waiting, run]):
            with self.subTest(first=rollup[0]["conclusion"]):
                self.assertEqual(pr_status.checks(rollup), ("wait 0ok/0bad/1wait", ["wait ci"]))
        for rollup in ([run, waiting, failed], [failed, run, waiting], [waiting, failed, run]):
            with self.subTest(first=rollup[0]["conclusion"]):
                self.assertEqual(pr_status.checks(rollup), ("FAIL 0ok/1bad/0wait", ["FAIL ci"]))
        # The same URL under another workflow or another check name is a different check.
        other = dict(failed, workflowName="Release")
        renamed = dict(run, name="lint")
        self.assertEqual(pr_status.checks([run, other, renamed])[0], "FAIL 2ok/1bad/0wait")

    def test_a_later_run_of_another_workflow_run_never_hides_a_failure(self):
        failed = {"__typename": "CheckRun", "name": "test", "workflowName": "CI", "conclusion": "FAILURE",
                  "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9",
                  "startedAt": "2026-01-01T10:00:00Z", "completedAt": "2026-01-01T10:05:00Z"}
        passed = dict(failed, conclusion="SUCCESS", detailsUrl="https://github.com/o/r/actions/runs/2/job/9",
                      startedAt="2026-01-01T10:05:00Z", completedAt="2026-01-01T10:09:00Z")
        for rollup in ([failed, passed], [passed, failed]):
            with self.subTest(first=rollup[0]["conclusion"]):
                self.assertEqual(pr_status.checks(rollup), ("FAIL 1ok/1bad/0wait", ["FAIL test"]))
        # A status context reads its URL from targetUrl; another or a missing URL never hides it.
        old = {"__typename": "StatusContext", "context": "ci", "state": "FAILURE",
               "targetUrl": "https://github.com/o/r/actions/runs/5", "startedAt": "2026-01-01T10:00:00Z"}
        for later in (dict(old, state="SUCCESS", targetUrl="https://x.test/b"),
                      dict(old, state="SUCCESS", targetUrl="https://github.com/o/r/actions/runs/6")):
            self.assertEqual(pr_status.checks([old, later])[0], "FAIL 1ok/1bad/0wait")

    def test_overlapping_runs_of_one_check_both_count(self):
        # A push run and a pull_request run of the same workflow job are not re-runs.
        failed = {"__typename": "CheckRun", "name": "ci", "workflowName": "CI", "conclusion": "FAILURE",
                  "startedAt": "2026-01-01T10:00:00Z", "completedAt": "2026-01-01T10:05:00Z"}
        passed = dict(failed, conclusion="SUCCESS",
                      startedAt="2026-01-01T10:00:01Z", completedAt="2026-01-01T10:06:00Z")
        for rollup in ([failed, passed], [passed, failed]):
            with self.subTest(first=rollup[0]["conclusion"]):
                self.assertEqual(pr_status.checks(rollup), ("FAIL 1ok/1bad/0wait", ["FAIL ci"]))

    def test_a_status_context_and_a_check_run_of_one_name_are_different_checks(self):
        status = {"__typename": "StatusContext", "context": "build", "state": "FAILURE",
                  "startedAt": "2026-01-01T10:00:00Z"}
        run = {"__typename": "CheckRun", "name": "build", "workflowName": "", "conclusion": "SUCCESS",
               "startedAt": "2026-01-01T11:00:00Z", "completedAt": "2026-01-01T11:05:00Z"}
        for rollup in ([status, run], [run, status]):
            with self.subTest(first=rollup[0]["__typename"]):
                self.assertEqual(pr_status.checks(rollup), ("FAIL 1ok/1bad/0wait", ["FAIL build"]))

    def test_a_failure_without_times_is_never_superseded(self):
        failed = {"__typename": "CheckRun", "name": "ci", "workflowName": "CI", "conclusion": "FAILURE"}
        passed = dict(failed, conclusion="SUCCESS",
                      startedAt="2026-01-01T11:00:00Z", completedAt="2026-01-01T11:05:00Z")
        for rollup in ([failed, passed], [passed, failed]):
            with self.subTest(first=rollup[0]["conclusion"]):
                self.assertEqual(pr_status.checks(rollup), ("FAIL 1ok/1bad/0wait", ["FAIL ci"]))
        # Nor does an entry without times supersede a timed failure.
        timed = dict(passed, conclusion="FAILURE")
        untimed = dict(failed, conclusion="SUCCESS")
        self.assertEqual(pr_status.checks([timed, untimed]), ("FAIL 1ok/1bad/0wait", ["FAIL ci"]))

    def test_a_later_status_context_without_a_start_still_counts(self):
        passed = {"__typename": "StatusContext", "context": "deploy", "state": "SUCCESS",
                  "startedAt": "2026-01-01T10:00:00Z"}
        failed = {"__typename": "StatusContext", "context": "deploy", "state": "FAILURE",
                  "startedAt": None}
        for rollup in ([passed, failed], [failed, passed]):
            with self.subTest(first=rollup[0]["state"]):
                self.assertEqual(pr_status.checks(rollup), ("FAIL 1ok/1bad/0wait", ["FAIL deploy"]))

    def test_identical_instant_runs_keep_exactly_one(self):
        run = {"__typename": "CheckRun", "name": "ci", "workflowName": "CI", "conclusion": "SUCCESS",
               "detailsUrl": "https://github.com/o/r/actions/runs/3/job/1",
               "startedAt": "2026-01-01T10:00:00Z", "completedAt": "2026-01-01T10:00:00Z"}
        self.assertEqual(pr_status.checks([run, dict(run, conclusion="FAILURE")]),
                         ("FAIL 0ok/1bad/0wait", ["FAIL ci"]))


class ResolveExecutableTests(unittest.TestCase):
    """Files are planted, never run: the lookup only inspects the file system."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.cwd = self.dir / "repo"
        self.bin = self.dir / "bin"
        for folder in (self.cwd, self.bin, self.cwd / "sub"):
            folder.mkdir()
        for name in ("gh", "gh.exe", "gh.com", "gh.cmd"):
            planted = self.cwd / name
            planted.write_text("planted")
            planted.chmod(0o755)
        old = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old)

    def test_planted_cwd_gh_is_never_chosen(self):
        exts = (".com", ".exe") if os.name == "nt" else ("",)
        path = os.pathsep.join(["", ".", "sub", str(self.cwd)])
        with patch.dict(os.environ, {"PATH": path}):
            self.assertIsNone(pr_status.resolve_executable("gh", exts))
        real = self.bin / ("gh.exe" if os.name == "nt" else "gh")
        real.write_text("tool")
        real.chmod(0o755)
        with patch.dict(os.environ, {"PATH": os.pathsep.join([".", str(self.cwd), str(self.bin)])}):
            self.assertEqual(pr_status.resolve_executable("gh", exts), str(real))

    @unittest.skipUnless(os.name == "nt", "cmd.exe wrappers exist only on Windows")
    def test_windows_gh_ignores_cmd_wrappers_on_path(self):
        (self.bin / "gh.cmd").write_text("wrapper")
        with patch.dict(os.environ, {"PATH": str(self.bin)}), \
             patch.object(pr_status.os.path, "expanduser", return_value=str(self.dir / "none")):
            self.assertIsNone(pr_status.wrapper_command())


if __name__ == "__main__":
    unittest.main()
