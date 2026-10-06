import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin/session-focus.py"
SPEC = importlib.util.spec_from_file_location("session_focus", SCRIPT)
focus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(focus)

SESSION = "366f2731-1b7b-4099-9607-d8e06526a63e"
PARENTS = {500: 400, 400: 300, 300: 200, 200: 300}  # a cycle above the extension host


class SessionFocusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "claude"
        self.home = self.root / "orchestrator"
        (self.config / "sessions").mkdir(parents=True)
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config),
                                      "ORCHESTRATOR_HANDOFF_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        grace = patch.object(focus.rollover, "CLAIMED_GRACE", 0.2)
        grace.start()
        self.addCleanup(grace.stop)
        self.starts = {}  # creation times by pid; empty reads as unknown

    def register(self, pid=500, entrypoint="claude-vscode", session=SESSION, name="soldi-flutter-44", **extra):
        (self.config / "sessions" / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": session, "cwd": "c:\\repo", "name": name,
            "entrypoint": entrypoint, "status": "idle", "updatedAt": 1791286092610, **extra,
        }), encoding="utf-8")

    def run_focus(self, opener=None, timeout=0.2):
        opener = opener or (lambda uri: self.fail("no URI expected"))
        with patch.object(focus.rollover, "parent_map", return_value=dict(PARENTS)):
            return focus.focus(SESSION, timeout, opener=opener, start=self.starts.get)

    def test_invalid_session_id_is_refused(self):
        self.assertEqual(focus.focus("not-a-session")[0], 1)

    def test_session_not_running(self):
        self.register(session="00000000-0000-4000-8000-000000000000")
        self.assertEqual(self.run_focus(), (1, "session not running"))

    def test_cli_session_is_switched_in_its_terminal(self):
        self.register(entrypoint="cli")
        self.assertEqual(self.run_focus(),
                         (1, "session soldi-flutter-44 runs in a terminal (cli); switch to it there"))

    def test_stale_registry_file(self):
        self.register(pid=999)
        self.assertEqual(self.run_focus(), (1, "session process 999 is gone (stale registry file)"))

    def test_live_entry_wins_over_a_stale_one(self):
        self.register(pid=999, entrypoint="cli")
        self.register(pid=500)
        seen = []

        def acknowledge(uri):
            request_id = uri.split("id=", 1)[1]
            seen.append(json.loads((self.home / "launches" / f"{request_id}.json").read_text()))
            focus.rollover.write_json(self.home / "acks" / f"{request_id}.json", {"status": "focused"})

        self.assertEqual(self.run_focus(acknowledge), (0, "focused soldi-flutter-44"))
        self.assertEqual(seen[0]["hosts"], [400, 300, 200])

    def test_live_cli_entry_beats_a_stale_vscode_one(self):
        self.register(pid=500, entrypoint="cli")
        self.register(pid=999)
        self.assertEqual(self.run_focus(),
                         (1, "session soldi-flutter-44 runs in a terminal (cli); switch to it there"))

    def test_ancestors_stop_at_three(self):
        chain = {pid: pid + 1 for pid in range(1, 11)}
        self.assertEqual(focus.ancestors(1, chain, start={}.get), [2, 3, 4])

    def test_old_bridge_error_gets_a_hint(self):
        self.register()

        def refuse(uri):
            request_id = uri.split("id=", 1)[1]
            (self.home / "launches" / f"{request_id}.json").unlink()
            focus.rollover.write_json(self.home / "acks" / f"{request_id}.json",
                                      {"status": "error", "error": "unknown handoff request"})

        code, message = self.run_focus(refuse)
        self.assertEqual(code, 1)
        self.assertIn("unknown handoff request (handoff bridge older than 0.6.0? reinstall it)", message)

    def test_multiline_name_is_one_line(self):
        self.register(entrypoint="cli", name="a\nb  c")
        self.assertEqual(self.run_focus(), (1, "session a b c runs in a terminal (cli); switch to it there"))

    def test_focus_is_acknowledged(self):
        self.register()
        seen = []

        def acknowledge(uri):
            self.assertTrue(uri.startswith("vscode://coding-orchestrator.handoff-bridge/open?id="))
            request_id = uri.split("id=", 1)[1]
            self.assertRegex(request_id, r"^[0-9a-f]{32}$")
            seen.append(json.loads((self.home / "launches" / f"{request_id}.json").read_text()))
            (self.home / "launches" / f"{request_id}.json").unlink()  # claimed by the bridge
            focus.rollover.write_json(self.home / "acks" / f"{request_id}.json",
                                      {"status": "focused", "session": SESSION})

        self.assertEqual(self.run_focus(acknowledge), (0, "focused soldi-flutter-44"))
        request = seen[0]
        self.assertEqual(request["action"], "focus")
        self.assertEqual(request["session"], SESSION)
        self.assertEqual(request["hosts"], [400, 300, 200])
        self.assertNotIn(500, request["hosts"])
        self.assertIsInstance(request["created_at"], float)
        self.assertEqual(list((self.home / "acks").iterdir()), [])

    def test_bridge_error_is_reported(self):
        self.register()

        def refuse(uri):
            request_id = uri.split("id=", 1)[1]
            (self.home / "launches" / f"{request_id}.json").unlink()
            focus.rollover.write_json(self.home / "acks" / f"{request_id}.json",
                                      {"status": "error", "error": "invalid or expired focus request"})

        code, message = self.run_focus(refuse)
        self.assertEqual(code, 1)
        self.assertIn("invalid or expired focus request", message)

    def test_no_ack_withdraws_the_request(self):
        self.register()
        code, message = self.run_focus(lambda uri: None)
        self.assertEqual(code, 1)
        self.assertIn("did not pick up the request", message)
        self.assertEqual(list((self.home / "launches").iterdir()), [])

    def acknowledge_hosts(self, seen):
        def acknowledge(uri):
            request_id = uri.split("id=", 1)[1]
            seen.append(json.loads((self.home / "launches" / f"{request_id}.json").read_text()))
            (self.home / "launches" / f"{request_id}.json").unlink()
            focus.rollover.write_json(self.home / "acks" / f"{request_id}.json", {"status": "focused"})
        return acknowledge

    def test_reused_pid_is_a_stale_registry_file(self):
        # The session died and another process now has its pid: creation times differ.
        self.register(procStart="134357575816763395")
        self.starts = {500: 134357575816763999, 400: 1}
        self.assertEqual(self.run_focus(), (1, "session process 500 is gone (stale registry file)"))

    def test_unreadable_creation_time_on_windows_is_not_proof_of_life(self):
        # Patching os.name makes pathlib build WindowsPath on POSIX, so stub it only around
        # is_alive (which builds no paths) and run the end-to-end check on real Windows.
        entry = {"pid": 500, "procStart": "134357575816763395"}
        with patch.object(focus.os, "name", "nt"):
            self.assertFalse(focus.is_alive(entry, dict(PARENTS), start=self.starts.get))
        with patch.object(focus.os, "name", "posix"):
            self.assertTrue(focus.is_alive(entry, dict(PARENTS), start=self.starts.get))
        if os.name == "nt":
            self.register(procStart="134357575816763395")
            self.assertEqual(self.run_focus(), (1, "session process 500 is gone (stale registry file)"))

    def test_matching_creation_time_is_live(self):
        self.register(procStart="1000")
        self.starts = {500: 1000, 400: 900, 300: 800, 200: 700}
        seen = []
        self.assertEqual(self.run_focus(self.acknowledge_hosts(seen)), (0, "focused soldi-flutter-44"))
        self.assertEqual(seen[0]["hosts"], [400, 300, 200])

    def test_walk_stops_at_a_parent_younger_than_its_child(self):
        # 300 is a dead parent's pid reused by a newer process: it is not an ancestor.
        self.register()
        self.starts = {500: 1000, 400: 900, 300: 5000, 200: 700}
        seen = []
        self.assertEqual(self.run_focus(self.acknowledge_hosts(seen)), (0, "focused soldi-flutter-44"))
        self.assertEqual(seen[0]["hosts"], [400])

    def test_walk_stops_at_an_unreadable_parent(self):
        self.assertEqual(focus.ancestors(500, dict(PARENTS), start={500: 1000, 400: 900}.get), [400])
        self.assertEqual(focus.ancestors(500, dict(PARENTS), start={}.get), [400, 300, 200])
        self.assertEqual(focus.ancestors(1, {1: 0}, start={}.get), [])

    def test_other_machine_is_refused(self):
        self.register(pidDomain="linux:elsewhere")
        self.assertEqual(self.run_focus(), (1, "session soldi-flutter-44 runs on another machine"))

    def test_this_machine_is_accepted(self):
        self.register(pidDomain=f"{focus.sys.platform}:{focus.socket.gethostname().lower()}")
        seen = []
        self.assertEqual(self.run_focus(self.acknowledge_hosts(seen)), (0, "focused soldi-flutter-44"))

    def test_launch_failure_withdraws_the_request(self):
        self.register()

        def broken(uri):
            raise OSError("no handler for vscode://")

        code, message = self.run_focus(broken)
        self.assertEqual(code, 1)
        self.assertIn("editor launch failed (no handler for vscode://)", message)
        self.assertEqual(list((self.home / "launches").iterdir()), [])

    def test_started_reads_this_process(self):
        if focus.os.name != "nt":
            self.assertIsNone(focus.started(os.getpid()))
        else:
            self.assertIsInstance(focus.started(os.getpid()), int)
            self.assertIsNone(focus.started(0x7FFFFFF0))

    def test_main_prints_one_line(self):
        self.register(entrypoint="cli")
        with patch.object(focus.rollover, "parent_map", return_value=dict(PARENTS)), \
                patch("builtins.print") as printed:
            self.assertEqual(focus.main(["--session", SESSION]), 1)
        printed.assert_called_once_with(
            "session soldi-flutter-44 runs in a terminal (cli); switch to it there")


if __name__ == "__main__":
    unittest.main()
