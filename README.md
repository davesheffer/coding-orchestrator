# Coding Orchestrator

An orchestrator and four reusable subagent roles for **Claude Code and Codex**.
The principle is **cheap hands, expensive eyes**: delegate bounded reading,
command execution, and implementation; keep design, judgment, and verification
with the orchestrator.

Every subagent reports `RESULT`, `EVIDENCE`, `CONFIDENCE`, and `UNVERIFIED`.
The orchestrator reads a builder's diff, escalates uncertain claims, and gets a
critic's review for risky work. Pushes, publishing, deployment, deletion, and
sending messages stay with the orchestrator and the user's authorization.

| Role | Responsibility | Claude Code | Codex model / reasoning |
|---|---|---|---|
| Orchestrator | Design, ambiguity, root causes, final verification | Fable | GPT-6 Astra / high |
| `scout` | Read-only lookup and reconnaissance | Sonnet | GPT-5.6 Luna / low |
| `runner` | Exact commands; exit codes and verbatim failures | Sonnet | GPT-5.6 Luna / low |
| `builder` | A specified change, then its acceptance check | Opus | GPT-5.6 Terra / medium |
| `critic` | Fresh-context adversarial review | Fable | GPT-6 Astra / high |

These are the author's model selections, not a promise of access on every plan.
Check your model picker and adjust the role definitions for your account.

## Install

```sh
git clone https://github.com/davesheffer/coding-orchestrator.git
cd coding-orchestrator
```

Install either setup independently, or both. The default destinations are global
assistant homes; neither installer implicitly configures a project's local files.

### Claude Code

```sh
./install.sh
```

Requires Claude Code and Python 3. The installer places the marked
orchestrator block, four Markdown roles, relay, and `pr-status` under
`${CLAUDE_HOME:-$HOME/.claude}`. It validates roles, JSON, markers, and destination
conflicts before writing, refuses symlinks, preserves instructions
outside its managed block, migrates old relay hooks to the selected
`CLAUDE_HOME`, keeps a tuned `relay/config.json`, and writes unique backups.

Use `./install.sh --dry-run` to inspect an installation. Bundle-managed files
update automatically when their installed hash still matches the manifest; a
local modification is never overwritten unless you review it and pass
`--force`. The installer is idempotent and does not copy credentials.

Hooks and installed instructions contain quoted absolute helper paths. The relay
finds its config, state, and handoffs next to its installed script, so an
installation-only `CLAUDE_HOME` does not need to remain in your shell. An explicit
runtime `CLAUDE_HOME` overrides this location. Hook migration matches complete
bundle commands and preserves other projects' relays and user-added commands.
Writes are atomic per file, not across the installation; an unexpected I/O error
can leave a partial install. Inspect backups and rerun after resolving the error.

Start a new Claude Code session after installation. The existing relay handles
the GREEN/AMBER/RED context gauge and session handoffs; see `relay/config.json`
for its thresholds. Roles have bounded turns, explicit permission modes, narrow
tool allowlists, and no MCP tools. Claude Code can still apply a stronger parent
permission mode, so use `/tasks` and `/status` to confirm the effective model and
settings when validating a new machine.

Each relay measurement reads at most the last **8 MiB** of the transcript. Missing
usage or a compaction boundary without subsequent usage yields **unknown**, never
a guessed zone or forced rollover. The relay's transcript format is a heuristic;
live client validation remains necessary after client upgrades.

### Codex

```sh
./codex/install.sh --dry-run
./codex/install.sh
```

Requires Python **3.11+** (`tomllib`) and a Codex version supporting native
`~/.codex/agents/*.toml` roles. Earlier work targeted **Codex CLI 0.154.0**;
Python tests do not establish native client compatibility or effective permissions.

The Codex installer:

- Installs four native TOML roles and a standalone copy of `bin/pr-status` under
  `${CODEX_HOME:-$HOME/.codex}`. Claude Code does not need to be installed.
- Merges the marked orchestrator section into global `AGENTS.md`, preserving
  content outside that section. Existing instructions receive a backup when
  changed. A global `AGENTS.override.md` is left alone and reported because it
  can shadow `AGENTS.md`.
- Creates `config.toml` from `codex/config.example.toml` only when absent.
  An existing configuration, including its model choices, is preserved exactly.
- Validates TOML, markers, and conflicts before writing. Differing role/helper
  files require `--force`, which saves unique backups. Malformed inputs and
  symlink destinations are refused, including with `--force`.
  Valid older role schemas can be upgraded with `--force`; the new role contract
  is enforced on incoming bundle files, not on files being replaced.
- Uses atomic replacement per file. Validation failures make no changes;
  unexpected I/O failures during installation can leave an incomplete install.
  Inspect the reported files and backups before rerunning.

To replace customized bundle-owned role/helper files after reviewing a diff:

```sh
./codex/install.sh --force
```

The installer never reads `auth.json`, copies credentials, writes to
`~/.claude`, or installs Codex hooks. It does not permanently change your shell's
PATH. If you already installed a helper as a symlink, the installer refuses it;
inspect and relocate that link before installing a regular copy.

Start a **new Codex session** from the project you want to work on:

```sh
codex -m gpt-6-astra -c 'model_reasoning_effort="high"'
```

Try: “Use scout to locate the task-report renderer. Explain its entry points;
don't edit anything.” For a normal implementation task, the global instructions
route bounded work to the appropriate role. See the official documentation for
[custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents) and
[global instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
`codex/PROMPT.md` provides a validation brief.

#### Codex permission limits

Scout and critic use `read-only`; runner and builder use `workspace-write`.
Runner is allowed to create artifacts from the exact test/build command it was
given, but it may not edit source, install dependencies, or repair failures.
Subagent network access is disabled unless explicitly authorized.

**Parent runtime overrides can supersede a role's sandbox settings.** Native
roles are not a universal isolation boundary. The instructions require checking
effective permissions and using a separately restricted `codex exec` process
when the host cannot enforce them. For builder, that process clears extra
writable roots and excludes temporary directories outside the workspace.
The orchestrator must disable every effective MCP server for an isolated launch;
the known names in the role files are not a wildcard for future servers.
This repository does not ship a permission-enforcing launcher, and a prompt
alone does not replace a sandbox. See `codex/AGENTS.md` for the launch requirements
and refusal behavior.

Codex uses native compaction. A transcript-based gauge is documented in
[the proposal](codex/CONTEXT-GAUGE.md); it is **not implemented or installed**.
The Claude relay remains Claude-specific.

## PR and CI status

The shared helper prints one compact table of open PRs and their checks:

```sh
# Codex installation
PATH="${CODEX_HOME:-$HOME/.codex}/bin:$PATH" pr-status

# Claude Code installation
"${CLAUDE_HOME:-$HOME/.claude}/bin/pr-status" --failed
```

It requires an authenticated `gh`. When executable, `~/.hunch/agent-gh` is used
instead; that wrapper is optional and is not bundled. Run this networked helper
as the orchestrator, unless network access has been explicitly granted to a
runner. No credentials are included.

## Bundle layout

| Files | Purpose |
|---|---|
| `CLAUDE.md`, `agents/*.md` | Claude orchestrator and bounded native roles |
| `install.sh`, `claude/install.py` | Claude installer with per-file atomic writes |
| `relay/`, `hooks.json` | Claude-only context relay |
| `codex/AGENTS.md`, `codex/agents/*.toml` | Codex orchestrator and native roles |
| `codex/install.sh`, `codex/install.py`, `codex/config.example.toml` | Codex installation and new-home defaults |
| `codex/PROMPT.md`, `codex/CONTEXT-GAUGE.md` | Validation brief and optional gauge proposal |
| `docs/codex-reference.md`, `docs/client-validation.md` | Port history and native client acceptance checks |
| `bin/pr-status` | Shared PR/CI helper |
| `tests/` | Bundle contracts, both installers, and relay behavior |
| `.github/workflows/ci.yml` | Python 3.11–3.13 CI, compilation, shell syntax, and tests |

## Update and verify

After `git pull`, rerun the installer for the assistant you use. Start with
`--dry-run`; review any local managed-file conflict before using `--force`.

Run the complete verification suite without touching your real assistant homes:

```sh
python3 -m unittest discover -s tests -v
```

The suite exercises clean installs, upgrades, idempotency, unique backups,
custom home paths, malformed inputs, symlink attacks, role parity, JSON/TOML
contracts, relay token zones, hook failure safety, and handoff persistence.
It also executes both shell entry points, upgrades previous-release Claude and Codex files,
preserves unrelated hooks, exercises custom-home handoffs without installation
environment variables, and verifies bounded transcript reads.

Before claiming compatibility with a client release, complete the
[native client acceptance checks](docs/client-validation.md). Those require
installed, authenticated clients and are separate from the offline Python suite.

Credentials, personal MCP configuration, per-project assistant configuration,
personal status lines, and permission allowlists do not belong in this bundle.

Previously named `claude-orchestrator`; the repository now covers both clients.
