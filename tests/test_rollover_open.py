import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin/rollover-open.py"
SPEC = importlib.util.spec_from_file_location("rollover_open", SCRIPT)
rollover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollover)


class RolloverOpenTests(unittest.TestCase):
    def test_windows_opener_uses_code_cli(self):
        with patch.object(rollover.sys, "platform", "win32"), \
                patch.object(rollover, "resolve_executable", return_value="code.cmd") as resolve, \
                patch.object(rollover.subprocess, "run") as run:
            rollover.open_uri("vscode://coding-orchestrator.handoff-bridge/open?id=test")
        resolve.assert_called_once_with("code")
        run.assert_called_once_with(
            ["code.cmd", "--open-url", "vscode://coding-orchestrator.handoff-bridge/open?id=test"],
            check=True, capture_output=True)

    def test_resolve_executable_ignores_cwd_and_relative_path_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            name = "code.exe" if os.name == "nt" else "code"
            tool = Path(temp) / name
            tool.write_text("x")
            tool.chmod(0o755)
            with patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", temp])}):
                self.assertEqual(Path(rollover.resolve_executable("code")), tool)
            with patch.dict(os.environ, {"PATH": os.pathsep.join(["", "."])}), \
                    patch.object(rollover.os, "getcwd", return_value=temp):
                self.assertIsNone(rollover.resolve_executable("code"))

    def test_stale_tmp_file_does_not_block_write_json(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "launches" / "a.json"
            path.parent.mkdir()
            (path.parent / "a.json.tmp").write_text("stale")
            rollover.write_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["a.json", "a.json.tmp"])

    def test_write_json_removes_tmp_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.json"
            with patch.object(rollover.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    rollover.write_json(path, {"ok": True})
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_launch_uses_opaque_id_and_waits_for_bridge_ack(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")

            def acknowledge(uri):
                request_id = uri.split("id=", 1)[1]
                self.assertRegex(request_id, r"^[0-9a-f]{32}$")
                self.assertNotIn(str(handoff), uri)
                request = json.loads((Path(temp) / "launches" / f"{request_id}.json").read_text())
                self.assertEqual(request["handoff"], str(handoff.resolve()))
                self.assertEqual(request["client"], "codex")
                rollover.write_json(Path(temp) / "acks" / f"{request_id}.json",
                                    {"status": "opened"})

            with patch.object(rollover, "open_uri", side_effect=acknowledge):
                result = rollover.launch("codex", handoff, timeout=0.1)
            self.assertIn("acknowledged", result)
            self.assertIn("Paste and send", result)

    def test_request_names_the_editor_hosts_that_may_open_it(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "editor_hosts", return_value=[4321, 8765]):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            seen = []

            def capture(uri):
                request_id = uri.split("id=", 1)[1]
                seen.append(json.loads((Path(temp) / "launches" / f"{request_id}.json").read_text()))

            with patch.object(rollover, "open_uri", side_effect=capture):
                rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertEqual(seen[0]["hosts"], [4321, 8765])

    def test_editor_hosts_are_ancestors_inside_an_extension_host(self):
        with patch.dict(os.environ, {"VSCODE_CRASH_REPORTER_PROCESS_TYPE": "extensionHost"}):
            hosts = rollover.editor_hosts()
        self.assertIn(os.getppid(), hosts)
        self.assertNotIn(os.getpid(), hosts)

    def test_editor_hosts_empty_outside_an_extension_host(self):
        env = {k: v for k, v in os.environ.items() if k != "VSCODE_CRASH_REPORTER_PROCESS_TYPE"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(rollover, "parent_map", side_effect=AssertionError("not needed")):
            self.assertEqual(rollover.editor_hosts(), [])

    def test_editor_hosts_survive_a_process_table_failure(self):
        with patch.dict(os.environ, {"VSCODE_CRASH_REPORTER_PROCESS_TYPE": "extensionHost"}), \
                patch.object(rollover, "parent_map", side_effect=OSError("denied")):
            self.assertEqual(rollover.editor_hosts(), [])

    def test_editor_hosts_stop_at_this_vscode_instance(self):
        parents = {100: 90, 90: 80, 80: 70, 70: 60}
        env = {"VSCODE_CRASH_REPORTER_PROCESS_TYPE": "extensionHost", "VSCODE_PID": "80"}
        with patch.dict(os.environ, env), \
                patch.object(rollover, "parent_map", return_value=parents), \
                patch.object(rollover.os, "getpid", return_value=100):
            self.assertEqual(rollover.editor_hosts(), [90, 80])

    def test_editor_hosts_cycle_without_vscode_pid_terminates(self):
        parents = {100: 90, 90: 80, 80: 90}
        env = {k: v for k, v in os.environ.items() if k != "VSCODE_PID"}
        env["VSCODE_CRASH_REPORTER_PROCESS_TYPE"] = "extensionHost"
        with patch.dict(os.environ, env, clear=True), \
                patch.object(rollover, "parent_map", return_value=parents), \
                patch.object(rollover.os, "getpid", return_value=100):
            self.assertEqual(rollover.editor_hosts(), [90, 80])

    def test_claude_terminal_request_carries_token_cwd_and_shells(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "editor_hosts", return_value=[]), \
                patch.object(rollover, "shell_ancestors", return_value=[4321, 8765]):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            seen = []

            def acknowledge(uri):
                request_id = uri.split("id=", 1)[1]
                seen.append(json.loads((Path(temp) / "launches" / f"{request_id}.json").read_text()))
                rollover.write_json(Path(temp) / "acks" / f"{request_id}.json",
                                    {"status": "opened", "client": "claude-terminal"})

            with patch.object(rollover, "open_uri", side_effect=acknowledge):
                result = rollover.launch("claude-terminal", handoff, "relay:abcd1234", timeout=0.1)
            self.assertIn("tab launch acknowledged", result)
            self.assertIn("VS Code terminal", result)
            request = seen[0]
            self.assertEqual(request["client"], "claude-terminal")
            self.assertEqual(request["token"], "relay:abcd1234")
            self.assertEqual(request["prompt"], "relay:abcd1234 continue from the saved handoff.")
            self.assertEqual(request["cwd"], os.getcwd())
            self.assertEqual(request["shells"], [4321, 8765])
            self.assertEqual(request["hosts"], [])

    def test_claude_terminal_unclaimed_request_falls_back_to_the_prompt(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "shell_ancestors", return_value=[]), \
                patch.object(rollover, "open_uri"):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\n", encoding="utf-8")
            result = rollover.launch("claude-terminal", handoff, "relay:abcd1234", timeout=0)
            self.assertNotIn("acknowledged", result)
            self.assertIn("Open a new Claude terminal and send: relay:abcd1234 continue", result)

    def test_terminal_clients_reject_malformed_tokens(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "open_uri", side_effect=AssertionError("no launch")), \
                patch.object(rollover.subprocess, "run", side_effect=AssertionError("no launch")):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\n", encoding="utf-8")
            for client in ("claude-terminal", "claude-wt"):
                for token in ("", "relay:abcd1234; rm -rf /", "relay:ABCD1234", "relay:abcd123",
                              "relay:abcd1234\n", "x relay:abcd1234"):
                    with self.subTest(client=client, token=token), self.assertRaises(ValueError):
                        rollover.launch(client, handoff, token)
            self.assertFalse((Path(temp) / "launches").exists())

    def test_shell_ancestors_only_inside_a_vscode_terminal(self):
        parents = {100: 90, 90: 80, 80: 90}
        with patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}), \
                patch.object(rollover, "parent_map", return_value=parents), \
                patch.object(rollover.os, "getpid", return_value=100):
            self.assertEqual(rollover.shell_ancestors(), [90, 80])
        with patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}), \
                patch.object(rollover, "parent_map", side_effect=OSError("denied")):
            self.assertEqual(rollover.shell_ancestors(), [])
        with patch.dict(os.environ, {"TERM_PROGRAM": "WezTerm"}), \
                patch.object(rollover, "parent_map", side_effect=AssertionError("not needed")):
            self.assertEqual(rollover.shell_ancestors(), [])

    def test_claude_wt_runs_windows_terminal_with_exact_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\n", encoding="utf-8")
            with patch.object(rollover, "resolve_executable", side_effect=lambda n: f"C:\\bin\\{n}.exe"), \
                    patch.object(rollover.subprocess, "run") as run, \
                    patch.object(rollover, "open_uri", side_effect=AssertionError("no bridge")):
                result = rollover.launch("claude-wt", handoff, "relay:abcd1234")
            run.assert_called_once_with(
                ["C:\\bin\\wt.exe", "-w", "0", "new-tab", "--title", "Claude relay", "-d", os.getcwd(),
                 "C:\\bin\\claude.exe", "relay:abcd1234 continue from the saved handoff."],
                check=True, capture_output=True, timeout=15)
            self.assertIn("tab launch acknowledged", result)
            self.assertFalse((Path(temp) / "launches").exists())

    def test_claude_wt_escapes_semicolons_in_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\n", encoding="utf-8")
            with patch.object(rollover, "resolve_executable", side_effect=lambda n: f"C:\\bin\\{n}.exe"), \
                    patch.object(rollover.os, "getcwd", return_value="C:\\a;b\\c"), \
                    patch.object(rollover.subprocess, "run") as run:
                rollover.launch("claude-wt", handoff, "relay:abcd1234")
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("-d") + 1], "C:\\a\\;b\\c")

    def test_claude_wt_unavailable_or_failed_falls_back_with_exit_2(self):
        with tempfile.TemporaryDirectory() as temp:
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\n", encoding="utf-8")
            command = 'claude "relay:abcd1234 continue from the saved handoff."'
            argv = ["open", "--client", "claude-wt", "--handoff", str(handoff),
                    "--resume-token", "relay:abcd1234"]
            failures = [
                (patch.object(rollover, "resolve_executable", return_value=None), "unavailable"),
                (patch.object(rollover.subprocess, "run",
                              side_effect=subprocess.CalledProcessError(1, "wt")), "failed"),
                (patch.object(rollover.subprocess, "run",
                              side_effect=subprocess.TimeoutExpired("wt", 15)), "failed"),
                (patch.object(rollover.subprocess, "run", side_effect=OSError("denied")), "failed"),
            ]
            for failure, word in failures:
                with self.subTest(word=word), \
                        patch.object(rollover, "resolve_executable", side_effect=lambda n: f"/bin/{n}"), \
                        failure, \
                        patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(rollover.main(argv), 2)
                self.assertIn(f"Windows Terminal launch {word}", output.getvalue())
                self.assertIn(f"run: {command}", output.getvalue())

    def test_launch_failure_after_the_bridge_claimed_still_reports_the_ack(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "CLAIMED_GRACE", 0.5):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")

            def claim_then_fail(uri):
                request_id = uri.split("id=", 1)[1]
                (Path(temp) / "launches" / f"{request_id}.json").unlink()
                rollover.write_json(Path(temp) / "acks" / f"{request_id}.json", {"status": "opened"})
                raise OSError("code exited late")

            with patch.object(rollover, "open_uri", side_effect=claim_then_fail):
                result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertIn("acknowledged", result)
            self.assertNotIn("Editor launch failed", result)

    def test_unclaimed_request_is_withdrawn_and_blames_the_bridge(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "open_uri"):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertIn("did not pick up the request", result)
            self.assertIn("vscode/handoff-bridge", result)
            self.assertIn("relay:1234abcd", result)
            self.assertNotIn("acknowledged", result)
            self.assertEqual(list((Path(temp) / "launches").iterdir()), [])

    def test_claimed_request_without_ack_never_claims_tab_opened(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "CLAIMED_GRACE", 0):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")

            def claim(uri):
                request_id = uri.split("id=", 1)[1]
                (Path(temp) / "launches" / f"{request_id}.json").unlink()

            with patch.object(rollover, "open_uri", side_effect=claim):
                result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertIn("not confirmed", result)
            self.assertIn("relay:1234abcd", result)

    def test_ack_after_claim_within_grace_is_reported(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "CLAIMED_GRACE", 5):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            timers = []

            def claim_then_ack_late(uri):
                request_id = uri.split("id=", 1)[1]
                (Path(temp) / "launches" / f"{request_id}.json").unlink()
                timer = threading.Timer(0.3, rollover.write_json,
                                        (Path(temp) / "acks" / f"{request_id}.json", {"status": "opened"}))
                timers.append(timer)
                timer.start()

            with patch.object(rollover, "open_uri", side_effect=claim_then_ack_late):
                result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            timers[0].join()
            self.assertIn("acknowledged", result)
            self.assertEqual(list((Path(temp) / "acks").iterdir()), [])

    def test_locked_request_is_withdrawn_once_the_lock_clears(self):
        real_unlink = Path.unlink
        attempts = []

        def locked_twice(path, *args, **kwargs):
            if path.parent.name == "launches" and len(attempts) < 2:
                attempts.append(path)
                raise PermissionError(32, "being used by another process")
            return real_unlink(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "open_uri"):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            with patch.object(Path, "unlink", autospec=True, side_effect=locked_twice):
                result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertEqual(len(attempts), 2)
            self.assertIn("did not pick up the request", result)
            self.assertEqual(list((Path(temp) / "launches").iterdir()), [])

    def test_request_that_stays_locked_warns_of_a_late_tab(self):
        def always_locked(path, *args, **kwargs):
            raise PermissionError(32, "being used by another process")

        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "open_uri"), \
                patch.object(rollover, "CLAIMED_GRACE", 0):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            with patch.object(Path, "unlink", autospec=True, side_effect=always_locked):
                result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertIn("could not be withdrawn", result)
            self.assertIn("may still open late", result)
            self.assertIn("relay:1234abcd", result)
            self.assertNotIn("acknowledged", result)

    def test_locked_request_still_reports_an_ack_already_written(self):
        def always_locked(path, *args, **kwargs):
            if path.parent.name == "launches":
                raise PermissionError(32, "being used by another process")

        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "CLAIMED_GRACE", 0):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            calls = []

            def ack_after_first_poll(path, seconds):
                calls.append(seconds)
                return {"status": "opened"} if len(calls) > 1 else None

            with patch.object(rollover, "open_uri"), \
                    patch.object(rollover, "wait_for_ack", side_effect=ack_after_first_poll), \
                    patch.object(Path, "unlink", autospec=True, side_effect=always_locked):
                result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertIn("acknowledged", result)

    def test_failed_launcher_removes_its_request(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}), \
                patch.object(rollover, "open_uri", side_effect=OSError("no handler")):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertIn("Editor launch failed", result)
            self.assertEqual(list((Path(temp) / "launches").iterdir()), [])

    def test_codex_handoff_saves_body(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}):
            path = rollover.save_codex("repair", "GOAL: continue the repair\nSTATE: tests passed\nNEXT STEP: verify")
            self.assertIn("GOAL: continue", path.read_text(encoding="utf-8"))
            self.assertEqual(path.parent, (Path(temp) / "handoffs").resolve())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_utf8_handoff_stdin_even_with_legacy_locale(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, ORCHESTRATOR_HANDOFF_HOME=temp, CODEX_HOME=str(Path(temp) / "codex"),
                       PYTHONIOENCODING="cp1255")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "handoff", "--client", "codex",
                 "--no-open", "--title", "עברית"],
                input="GOAL: להמשיך את העבודה\nSTATE: handoff saved\nNEXT STEP: verify".encode("utf-8"),
                capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = next((Path(temp) / "handoffs").glob("*.md"))
            self.assertIn("להמשיך", saved.read_text(encoding="utf-8"))
            self.assertIn(f"Open a new Codex session and send: Continue from the attached handoff file {saved.resolve()}",
                          result.stdout.decode("utf-8", "replace"))
