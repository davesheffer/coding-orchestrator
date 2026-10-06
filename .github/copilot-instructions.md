# Copilot instructions

<!-- HUNCH:START — auto-generated, do not edit by hand -->
<!-- hunch:template 2 -->
## 🧠 Hunch (Engineering Memory)

This repo has **Hunch**, a graph of *why* the code is the way it is. It holds **0 decisions, 0 bugs, 0 constraints, 0 components, 0 policies**. Use the `hunch_*` MCP tools by moment:

- **Start:** reuse the task ID and `task verify` command the prompt hook printed; with none, call `hunch_task(action: "start", title)` once. Then `hunch_context(target, task_id)` first. Orient with `hunch_structure`, `hunch_workspaces`, `hunch_runbook(task)`. Ask the user about each `hunch_escalations()` entry; silence is never approval.
- **Design:** `hunch_why(target)` (includes what was rejected), `hunch_current_decision(topic)`, `hunch_bug_lineage(symptom_or_symbol)`, `hunch_compare(candidates)`, `hunch_query(query)`.
- **Edit:** `hunch_check_constraints(scope)`, `hunch_get_dependents(symbol)` / `hunch_blast_radius(target)`, `hunch_findings(scope?)`.
- **Merge:** `hunch_conformance()`, `hunch_pr_impact(base?)`, `hunch_merge_verdict`.
- **Record:** `hunch_capture_decision` → `hunch_record_decision`; `hunch_record_correction` turns a human correction into an enforced rule; `hunch_record_finding` keeps an observation with evidence. Pass the task_id.
- **Finish:** run checks through the `task verify` launcher. If the task used Hunch, you started it, or no host stop hook closes it, call `hunch_task(action: "finish", task_id)` and show its card verbatim. Its `applications` schema carries the claim rules.
- To update Hunch, run `hunch update` from the repo root.

_Records carry provenance and confidence; treat low-confidence items as advisory._
<!-- HUNCH:END -->
