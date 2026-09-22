import contextlib
import importlib.machinery
import importlib.util
import io
import json
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

    def test_invalid_arguments_do_not_query_github(self):
        for argv in (["--limit", "0"], ["--limit", "-1"], ["--limit", "abc"], ["--typo"], ["-R"]):
            with self.subTest(argv=argv), patch.object(pr_status, "gh") as gh:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    pr_status.main(argv)
                self.assertEqual(error.exception.code, 2)
                gh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
