import importlib.util
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
                patch.object(rollover.shutil, "which", return_value="code.cmd"), \
                patch.object(rollover.subprocess, "run") as run:
            rollover.open_uri("vscode://coding-orchestrator.handoff-bridge/open?id=test")
        run.assert_called_once_with(
            ["code.cmd", "--open-url", "vscode://coding-orchestrator.handoff-bridge/open?id=test"],
            check=True, capture_output=True)

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
