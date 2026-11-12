# ADR 0001: Enforce the push gate at the git pre-push hook, not in shell text

- Status: Proposed; draft implementation on feat/pre-push-gate
- Date: 2026-11-11
- Related: `bin/jev-guard.py` (`risk_gate`), [reference.md](../reference.md) "risk_gate is advisory, not enforcement", parked branch `fix/review-jev` (832bcd5)

## Context

`risk_gate` runs as a `PreToolUse` `Bash` hook. It finds `git commit` and
`git push` by parsing the command text the agent is about to run, then asks
Jev whether the change needs a critic. The reference already lists known
bypasses: `sh -c`, `eval`, aliases, functions, `xargs`, `$GIT push`, heredoc
bodies, separate script files, `GIT_DIR` overrides.

Branch `fix/review-jev` tried to close those bypasses inside the parser. Over
ten builder/critic rounds it added about 2,300 lines to `jev-guard.py`
(re-splitting runners, heredoc bodies, file writes later executed by `make`,
PowerShell string building, `.git/hooks` writes, fed groups). Every round
fixed the listed rows and every critic round found new misses on close
variants, including rows the base version caught. Round 10 also added a
super-linear regex (6-9 s at 50k chars). The work is parked unmerged.

The root problem: shell text is a full programming language. Any static
reading of it has an unbounded set of spellings that run `git push` (write a
file, then run it; build the word from parts; run it through another
interpreter). A text parser cannot be the enforcement point.

## Decision

1. Move enforcement of "no unreviewed push" to the boundary where the push
   actually happens: a git `pre-push` hook. Git calls it with the exact refs
   and commit ranges being pushed on stdin (`<local ref> <local sha> <remote
   ref> <remote sha>`), however the command was spelled.
2. The hook computes the pushed range, checks the critic ledger for the
   changed files, asks Jev for the risk category when needed, and exits
   non-zero to refuse an unreviewed risky push. Same one-retry semantics as
   today are optional and decided separately.
3. Keep `risk_gate` in `PreToolUse` as an early, advisory warning with its
   current scope (base 277c4ab). Do not grow its parser further to chase
   bypasses; new bypass reports go to the residuals list, not to new
   patterns.
4. The critic ledger moves from per-session state to a repo-scoped file
   keyed by file content hash, so a hook without a session id can read it.

## Consequences

- One code path sees every push, including those started from scripts,
  `make`, aliases, `eval` or another shell. The parser stays small and fast.
- `git push --no-verify` skips the hook. Mitigations, weakest to strongest:
  a narrow `PreToolUse` check for `--no-verify` and `core.hooksPath`
  changes (still text, but two fixed tokens instead of an open set); a
  Claude Code permission deny rule; server-side branch protection and
  required reviews, which are the only real guarantee.
- The hook must be installed per repository or through `core.hooksPath`.
  A global `core.hooksPath` replaces repository hooks (husky, lefthook), so
  the installer must chain to an existing hook rather than override it.
- An agent that can write `.git/hooks` or git config can remove the hook.
  This is the same trust level as today; server-side protection covers it.
- Commits are no longer gated at commit time unless a `pre-commit` hook does
  the same; the push gate is the one that matters for publishing.

## Alternatives considered

- Keep hardening the shell-text parser (rejected): ten rounds showed no
  convergence; each fix created regressions and new miss classes, and cost
  grows with every pattern.
- Run the command in a sandbox and intercept `git` on PATH with a wrapper
  (rejected for now): bypassed by absolute paths and other git builds, and
  platform-specific on Windows.
- Server-side only (branch protection) (kept as the backstop, not the
  primary gate): it cannot ask Jev or check the critic ledger before the
  push leaves the machine.

## Open questions for the owner

- Does the hook deny once and allow an identical retry, as today, or deny
  until a critic run is recorded? (draft default) Deny until the critic
  ledger covers the files; a person overrides with `git push --no-verify`.
  When Jev is off or unavailable the hook warns and allows the push.
- Install through `core.hooksPath` with chaining, or per repo on request?
  (draft default) Per repository on request:
  `claude/install.py --pre-push-hook <repo>` writes the repository's hook
  (honouring its own `core.hooksPath`), chains an existing hook as
  `pre-push.d/pre-push` (keeping its name), and never sets `core.hooksPath`
  globally; it refuses a global or system `core.hooksPath` and a hooks
  directory tracked by git.
- Should `pre-commit` mirror the gate, or is the push gate enough?
  (draft default) The push gate is enough; `risk_gate` keeps its advisory
  commit check.
- What does a critic run record in the ledger? (owner decision) Only what the
  critic was handed, named by a `REVIEW-SCOPE:` line in its prompt, in the
  repository that line names, and nothing on a blocking verdict.
