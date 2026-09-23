# Route comparison protocol

The six questions in `claude-readonly.json` are a small, deterministic lookup set. They are useful for checking whether a model can find and cite current repository behavior; they are not a substitute for real user tasks or blind review.

Run the paired, read-only clients with new private output paths:

```sh
python bin/compare-claude-readonly.py --output <private-path>/claude-readonly.json
python bin/compare-codex-readonly.py --output <private-path>/codex-readonly.json
```

The Claude comparison limits each call to read tools and a USD budget. The Codex comparison uses the Windows launcher, its normal permission probes, and `--trial-model`, which accepts only models already configured for the scout role and disables automatic model fallback for that run. Both tools store answer text in the requested private file so a reviewer can inspect correctness. Do not publish those files without reviewing their contents. Literal `required_hits` are a cue for manual review, **not** a correctness grade.

The specified-edit fixtures in `fixtures/` were copied to separate disposable workspaces for Sonnet and Opus. Each model could edit only `solution.py` through Claude's restricted file tools; an external runner executed `python -m unittest -q`. The `summary` fixture gained missing-record cases after the first pass, so the repair measurement is reported separately. The fixture tests are intentionally small and do not measure large changes, integration, or security-sensitive edits.

## 2026-09-23 local pilot

| Comparison | Observed result | Limit |
|---|---|---|
| Claude Haiku vs Sonnet, six lookups | Haiku $0.187, median 19.0 s; Sonnet $0.294, median 16.3 s. Both had a partial error in the network-consent answer. | Same repository and questions; no blind human grader. |
| Codex Luna vs Sol, six lookups | Both completed six. Luna's relay answer cited a previous-release fixture rather than current source; Sol cited current source. Median model times were 14.0 s and 15.3 s respectively. | CLI completion is not correctness. API token prices do not establish subscription cost. |
| Claude Sonnet vs Opus, two specified edits | Original tests passed for both. Expanded invalid-record tests exposed one Sonnet error; a Sonnet repair passed. Total Sonnet cost/time including repair: about $0.106 / 42 s; Opus: about $0.230 / 51 s. | Synthetic functions, four model runs and one repair. |

The raw local pilot artifacts are in the user's private `~/.codex/agent-runs/` directory. Model, version, pricing, and task mix can change; repeat on a broader frozen task set before claiming general savings.
