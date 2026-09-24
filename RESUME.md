# RESUME — unreviewed WIP, do not merge this branch

Goal: finish audit issues #12-#29. Merge PR #34 (fix/jev-guard), then PR #35 (fix/jev-router), then a #25 PR, reconcile the open issues, and write a final report. The user authorized deciding and merging ("make the best decision, i need a clean flow").

## Where things are
- main is green. #41 is merged (pr-status wrapper on Python 3.11).
- `wip/jev-guard` = fix/jev-guard @ 8b1a80d + one UNREVIEWED commit: the scanner's `frames` paren stack (only an arithmetic frame takes `))`; `<<` is a heredoc only outside arithmetic; `$[ ]` counts as arithmetic), plus keeping quoted `--git-dir`/`--work-tree`/... values. Tests are added. The full suite passed (370 OK). A Fable critic has NOT reviewed it yet. PR #34 itself is still at 8b1a80d, with CI green.
- `wip/jev-router` = fix/jev-router @ 7e01bd2 + one PARTIAL, UNTESTED builder commit for the #35 critic findings:
  1. A huge int confidence (10**400) raises OverflowError in bin/jev-route.py (~165) and codex/jev-hook.py (~109) -> add a shared `coerce_confidence(raw)` (rejects bool/str, try/except OverflowError, finite, [0,1]).
  2. bin/eval-jev-routing.py (~113) must use that same coercion, so applied accuracy matches production.
  3. Sanitise `probabilities` before logging (dict only, non-finite -> None). In eval, keep only a dict (a list [NaN] crashes --json).
  4. The safe_repr docstring in bin/jev_client.py says "ASCII-safe" -> "UTF-8-safe".
  Tests for all four are still missing.

## Next steps
1. `git fetch`. Get fix/jev-guard to the wip/jev-guard content (drop RESUME.md). Run a fresh Fable critic on the diff 8b1a80d..wip. Resolve its findings, then run `python -m unittest discover -s tests -q`, commit on fix/jev-guard, push, wait for CI, and `gh pr merge 34 --merge`. The jev risk gate hook blocks unreviewed commits: run the critic first.
2. On fix/jev-router: finish the 4 fixes from the wip commit and add tests. Run the suite and `python bin/eval-jev-routing.py --dry-run`, then get a Fable critic. Rebase on origin/main after #34 merges (bb66593 is already in #34), push with --force-with-lease, wait for green CI, and merge #35.
3. #25, after #35: in benchmarks/jev-routing.json, change `tricky-scout-debug` expected from opus to fable. Confirm that #35 covers the pinned-critic cases and applied accuracy, then run the dry-run eval and the suite, and open a PR "Fixes #25". #35's commit says "Fixes #14 #25", so reopen #25 if GitHub auto-closes it.
4. `gh issue list --state open`: close #12-#29 items that are fixed, with a reference. Follow-ups already filed: #36-#40, #42.
5. Delete the wip/jev-guard and wip/jev-router branches. Tell the user which local worktrees/branches of merged PRs to remove (fix/relay, fix/agent-run, fix/install-docs, fix/pr-status-py311). Final report.

## Rules
- Every fix commit gets a fresh Fable critic (subagent_type critic, model fable). Merge only on APPROVE / APPROVE-WITH-MINOR. Known follow-ups are out of scope for the critic.
- Scanner: a spurious detection is safe; a missed detection, wrong cwd, or >2 s scan fails open, which is a security failure.
- Commits end "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>". PR/issue bodies end "🤖 Generated with [Claude Code](https://claude.com/claude-code)". Preserve file line endings.
