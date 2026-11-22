import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("usage_report", ROOT / "bin" / "usage-report.py")
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)

NOW = "2099-01-01T00:00:00Z"


def assistant(message_id, model, inp=0, cw=0, cr=0, out=0, stamp=NOW, text="secret prompt text"):
    return {"type": "assistant", "timestamp": stamp, "message": {
        "id": message_id, "model": model, "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw,
                  "cache_read_input_tokens": cr, "output_tokens": out}}}


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "not json\n", encoding="utf-8")


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        project = self.root / "proj-a"
        write_jsonl(project / "s1.jsonl", [
            {"type": "user", "message": {"content": "hi"}},
            assistant("m1", "claude-opus-5-5", inp=1_000_000, out=100_000),
            assistant("m1", "claude-opus-5-5", inp=1_000_000, out=100_000),  # streamed chunk: once
            assistant("m2", "<synthetic>", inp=5),
        ])
        agents = project / "s1" / "subagents"
        write_jsonl(agents / "agent-a1.jsonl", [assistant("c1", "claude-fable-5-2", cw=1_000_000, cr=1_000_000)])
        (agents / "agent-a1.meta.json").write_text(json.dumps({"agentType": "critic", "model": "fable"}))
        write_jsonl(agents / "agent-a2.jsonl", [assistant("c2", "claude-fable-5-2", out=10)])
        (agents / "agent-a2.meta.json").write_text(json.dumps({"agentType": "critic"}))
        write_jsonl(agents / "agent-b1.jsonl", [assistant("b1", "claude-sonnet-5-5", out=1_000_000),
                                                assistant("b2", "mystery-model", out=7)])
        (agents / "agent-b1.meta.json").write_text("{broken")
        write_jsonl(self.root / "proj-b" / "s2.jsonl", [assistant("x1", "claude-haiku-5-5", out=1_000_000),
                                                        assistant("old", "claude-opus-5-5", out=9,
                                                                  stamp="2000-01-01T00:00:00Z")])

    def run_report(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report.main(["--projects-root", str(self.root), *args])
        return code, out.getvalue()

    def test_json_summary_dedupes_prices_and_attributes_roles(self):
        code, out = self.run_report("--json")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        rows = {(r["role"], r["model"]): r for r in summary["rows"]}
        main = rows[("main", "claude-opus-5-5")]
        self.assertEqual((main["input"], main["output"], main["turns"]), (1_000_000, 100_000, 1))
        self.assertEqual(main["usd"], 6.0)  # 1M in x $4 + 0.1M out x $20
        self.assertEqual(rows[("critic", "claude-fable-5-2")]["usd"], 16.2004)  # 2x$8 write + $0.20 read
        self.assertEqual(rows[("unknown", "claude-sonnet-5-5")]["usd"], 10.0)
        self.assertEqual(rows[("main", "claude-haiku-5-5")]["usd"], 0.5)
        self.assertNotIn(("main", "<synthetic>"), rows)
        self.assertEqual(summary["unpriced"], ["mystery-model"])
        self.assertEqual(summary["total_usd"], 32.7)
        roles = {r["role"]: r for r in summary["roles"]}
        self.assertEqual((roles["critic"]["runs"], roles["main"]["runs"]), (2, 2))
        self.assertEqual(summary["runs_per_session"]["critic"], {"sessions": 1, "median": 2, "p90": 2, "max": 2})
        requested = {(r["role"], r["launched_with"]): r["runs"] for r in summary["launched_models"]}
        self.assertEqual(requested[("critic", "fable")], 1)
        self.assertEqual(requested[("critic", "default")], 1)
        self.assertNotIn("secret prompt text", out)

    def test_cache_writes_priced_by_ttl_split(self):
        record = assistant("t1", "claude-opus-5-5", cw=1_000_000)
        record["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 600_000,
                                                        "ephemeral_1h_input_tokens": 400_000}
        write_jsonl(self.root / "proj-c" / "s3.jsonl", [record])
        summary = json.loads(self.run_report("--json", "--project", "proj-c")[1])
        self.assertEqual(summary["rows"][0]["usd"], 6.2)  # 0.6M x $4 x 1.25 + 0.4M x $4 x 2

    def test_workflow_agents_count_under_their_session(self):
        agents = self.root / "proj-c" / "s4" / "subagents" / "workflows" / "wf_1"
        write_jsonl(agents / "agent-w1.jsonl", [assistant("w1", "claude-haiku-5-5", out=1_000_000)])
        (agents / "agent-w1.meta.json").write_text(json.dumps({"agentType": "scout"}))
        summary = json.loads(self.run_report("--json", "--project", "proj-c")[1])
        self.assertEqual([(r["role"], r["usd"]) for r in summary["rows"]], [("scout", 0.5)])
        self.assertEqual(summary["runs_per_session"]["scout"]["sessions"], 1)

    def test_old_records_and_project_filter(self):
        summary = json.loads(self.run_report("--json", "--project", "proj-b")[1])
        self.assertEqual([(r["role"], r["model"]) for r in summary["rows"]], [("main", "claude-haiku-5-5")])

    def test_stale_files_are_skipped_by_mtime(self):
        old = time.time() - 40 * 86400
        for path in self.root.rglob("*.jsonl"):
            os.utime(path, (old, old))
        summary = json.loads(self.run_report("--json")[1])
        self.assertEqual((summary["rows"], summary["total_usd"]), ([], 0))

    def test_text_render_lists_roles_and_assumptions(self):
        code, out = self.run_report()
        self.assertEqual(code, 0)
        self.assertIn("not a subscription bill", out)
        self.assertIn("critic", out)
        self.assertIn("unpriced models (counted as $0): mystery-model", out)
        self.assertNotIn("secret prompt text", out)

    def test_bad_arguments_exit_2(self):
        for args in (("--days", "0"), ("--project", "nope")):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.run_report(*args)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
