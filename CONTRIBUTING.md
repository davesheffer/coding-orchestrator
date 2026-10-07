# Contributing

Thanks for helping! This repo makes AI coding assistants (Claude Code and Codex)
work like a team: one smart "lead" plans and checks the work, and cheaper
helpers do the reading, running, and building. Your change should keep that
promise: nothing gets called done until it has been checked.

## How a change gets in

1. **Fork** the repo (or make a branch, if you have write access).
2. Make your change on a branch with a clear name, like `fix/relay-windows-path`.
3. **Run the checks** below on your machine.
4. Open a **pull request** into `main`. Say what you changed and how you tested it.
5. CI runs on Ubuntu, Windows, and macOS. All 7 `verify` checks must pass.
6. A maintainer reviews it. You need **1 approval**, and every review comment
   must be resolved. A new push after approval needs a fresh approval.
7. It is **squash-merged**, so your PR becomes one commit on `main`.

You can't push to `main` directly; everything goes through a PR. First-time
contributors: a maintainer has to click "approve" before CI runs on your PR.
That's normal.

## Setup

You need **Python 3.11+** and **Node.js** (for the VS Code bridge test).
Nothing else to install: the tests use only the Python standard library.

```bash
git clone https://github.com/<you>/coding-orchestrator.git
cd coding-orchestrator
```

## Run the checks (same as CI)

```bash
python -m compileall -q claude codex relay tests bin/pr-status bin/agent-run.py bin/rollover-open.py
bash -n install.sh codex/install.sh
python -m unittest discover -s tests -v
node vscode/handoff-bridge/test.js
```

Changed the Mission Control mod (`mods/mission-control/`)? Also run:

```bash
claude plugin validate mods/mission-control
claude plugin test mods/mission-control
```

Test installers safely with `./install.sh --dry-run` (or
`python claude/install.py --dry-run` on Windows). It shows what would change
without touching your setup.

## Where things live

| Folder | What's inside |
|---|---|
| `claude/` | Claude Code installer, roles, and hooks |
| `codex/` | Codex installer and its roles |
| `relay/` | Hands a long chat off to a fresh session |
| `bin/` | Small helper commands (PR status, rollover, Jev) |
| `mods/` | The Mission Control pane for Claude Code |
| `vscode/` | VS Code extension that opens handoff tabs |
| `tests/` | Python tests for all of the above |
| `docs/` | Longer explanations and research |

## Rules of thumb

- **Add a test** for anything you fix or add. A bug fix should come with a test
  that fails without the fix.
- **Keep changes small.** One idea per PR is easier to review and to undo.
- **Don't fill in the placeholders** like `__PYTHON__` or `__RELAY__` in
  `CLAUDE.md` and `AGENTS.md`. The installer replaces them with real paths on
  each machine.
- **Never commit secrets** (API keys, tokens, `.env` files). Push protection
  will block most of them, but don't rely on it.
- **Installers must stay safe to rerun.** They should never delete a user's own
  files or copy credentials.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `fix(relay): ...`, `feat(mods): ...`, `docs: ...`.

## Reporting a bug

Open an issue with: what you ran, what you expected, what happened (paste the
exact error), your OS, Python version, and Claude Code or Codex version.

## License

By contributing, you agree that your work is released under the
[MIT License](LICENSE).
