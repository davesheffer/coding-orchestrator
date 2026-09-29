import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
                patch.dict(os.environ, {"ORCHESTRATOR_HANDOFF_HOME": temp}):
            handoff = Path(temp) / "handoff.md"
            handoff.write_text("GOAL: continue\nSTATE: saved\n", encoding="utf-8")
            request_ids = []

            def claim(uri):
                request_ids.append(uri.split("id=", 1)[1])
                (Path(temp) / "launches" / f"{request_ids[0]}.json").unlink()

            def late_ack(path, seconds):
                if seconds == rollover.CLAIMED_GRACE:
                    return {"status": "opened"}
                return None

            with patch.object(rollover, "open_uri", side_effect=claim), \
                    patch.object(rollover, "wait_for_ack", side_effect=late_ack):
                result = rollover.launch("claude", handoff, "relay:1234abcd", timeout=0)
            self.assertIn("acknowledged", result)

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
