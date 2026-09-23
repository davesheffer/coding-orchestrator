# Coding Orchestrator

[![CI](https://github.com/davesheffer/coding-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/davesheffer/coding-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An orchestrator and four reusable subagent roles for **Claude Code and Codex**.
The principle is **cheap hands, expensive eyes**: delegate bounded reading,
command execution, and implementation; keep design, judgment, and verification
with the orchestrator.

**Experimental release.** This is a configurable workflow bundle for people who
are comfortable reviewing assistant settings. Installers and helper behavior
have automated coverage; model access, native delegation, and isolation depend
on your client and environment. Check the [validation record](docs/client-validation.md)
before relying on a particular platform or client version.

Every subagent reports `RESULT`, `EVIDENCE`, `CONFIDENCE`, and `UNVERIFIED`.
The orchestrator reads a builder's diff, escalates uncertain claims, and gets a
critic's review for risky work. Pushes, publishing, deployment, deletion, and
sending messages stay with the orchestrator and the user's authorization.

| Role | Responsibility | Claude Code | Codex model / reasoning |
|---|---|---|---|
| Orchestrator | Design, ambiguity, root causes, final verification | Opus 5.5 | GPT-6 Sol / medium |
| `scout` | Read-only lookup and reconnaissance | Sonnet | GPT-6 Luna / low |
| `runner` | Exact commands; exit codes and verbatim failures | Sonnet | GPT-6 Luna / low |
| `builder` | A specified change, then its acceptance check | Sonnet | GPT-6 Sol / medium |
| `critic` | Fresh-context adversarial review | Fable | GPT-6 Astra / high |

The cross-client tier mapping is **Sol → Opus 5.5**, **Astra → Fable**,
**Tera → Sonnet**, and **Luna → Sonnet**. Tera has no separate bundled role;
the mapping applies if that tier is used for a future role.

The Claude installer sets `claude-opus-5-5` as the main-session default when no
model is already selected in user settings; existing explicit choices remain
untouched. Its named scout, runner, and builder roles use Sonnet, and its critic
uses Fable. [Claude Code 2.1.280 or later](https://code.claude.com/docs/en/model-config)
is required for Opus 5.5. These are routing defaults, not a promise of access on every plan.
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

Requires Claude Code and Python **3.10+**. The installer places the marked
orchestrator block, four Markdown roles, relay, and `pr-status` under
`${CLAUDE_HOME:-$HOME/.claude}`. It validates roles, JSON, markers, and destination
conflicts before writing, refuses symlinks, preserves instructions
outside its managed block, migrates old relay hooks to the selected
`CLAUDE_HOME`, keeps a tuned `relay/config.json`, and writes unique backups.
Known legacy files are recognized with either LF or CRLF line endings. Existing
Windows manifests remain readable; new manifests use portable `/` path separators.

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

On Windows, use `python claude/install.py --dry-run` followed by
`python claude/install.py`. Claude's generated hooks still require a POSIX shell
and a working `python3` command (for example, through Git Bash); native PowerShell
installation alone does not verify those hooks. For a custom destination, set
`CLAUDE_CONFIG_DIR` to the same directory as `CLAUDE_HOME` when launching Claude.

Each relay measurement reads at most the last **8 MiB** of the transcript. Missing
usage or a compaction boundary without subsequent usage yields **unknown**, never
a guessed zone or forced rollover. The relay's transcript format is a heuristic;
live client validation remains necessary after client upgrades.
Handoff files and relay input/output use UTF-8, including on Windows.
Legacy handoffs can still be read using the machine's local encoding. If a
handoff was moved from a different locale and cannot be decoded, the hook reports
that it needs conversion instead of silently discarding its contents.

### Codex

```sh
./codex/install.sh --dry-run
./codex/install.sh
```

Requires Python **3.11+** (`tomllib`) and a Codex version supporting native
`~/.codex/agents/*.toml` roles. Earlier work targeted **Codex CLI 0.154.0**;
Python tests do not establish native client compatibility or effective permissions.

The Codex installer:

- Installs four native TOML roles, `bin/pr-status`, the restricted Windows
  `bin/agent-run.py` launcher, and `bin/agent-report.py` under
  `${CODEX_HOME:-$HOME/.codex}`. Claude Code does not need to be installed.
- Merges the marked orchestrator section into global `AGENTS.md`, preserving
  content outside that section. Existing instructions receive a backup when
  changed. A global `AGENTS.override.md` is left alone and reported because it
  can shadow `AGENTS.md`.
- Creates `config.toml` from `codex/config.example.toml` only when absent.
  Fresh installs default generic subagents to Luna/low. Existing configurations
  are preserved exactly unless `--configure-routing` is selected; that option
  fills missing generic model/effort defaults while preserving explicit choices.
- Creates a user-owned `agent-routing.json` with no network-fallback approvals.
  Existing policies are validated and preserved, including on forced upgrades.
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

On Windows, use `python codex/install.py --dry-run` followed by
`python codex/install.py`. Invoke the installed helper as
`python "$env:USERPROFILE/.codex/bin/pr-status"` in PowerShell, or substitute
your custom `CODEX_HOME` path.

Start a **new Codex session** from the project you want to work on:

```sh
codex -m gpt-6-sol -c 'model_reasoning_effort="medium"'
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
The bundled Windows launcher probes the filesystem boundary, checks effective
MCP/tool restrictions, and records actual model usage. The known MCP names in role
files are not a wildcard for future servers. The launcher remains dependent on
the host's Codex sandbox; a prompt alone does not replace enforcement. Other
platforms retain the verified native/manual launch workflow. See
[routing and fallbacks](docs/agent-routing.md) for commands and limitations.
The [optimization audit](docs/optimization-research.md) records the current routing
change, measured limits, source research, and the experiments needed to compare
cost per correctly completed task.

On native Windows, verify the sandbox and the active Windows Firewall profile.
The `unelevated` sandbox provides weaker network isolation; the `elevated`
sandbox relies on firewall rules. If command network blocking cannot be verified,
the orchestrator must report that limitation and keep the work in the main
session, unless you explicitly authorize a different boundary. The installer
does not change firewall settings. See the [Windows sandbox documentation](https://learn.chatgpt.com/docs/windows/windows-sandbox).

An [optional last-resort network fallback](docs/agent-routing.md#optional-network-exception)
can be approved per role. The launcher tries isolation first on every run and
retains file-access limits and disabled external tools. Installation grants no
exception. Model-unavailable errors before any work can try Luna -> Sol
for scout/runner; builder uses Sol only. Tests, partial work and unknown
errors are never blindly retried. The older
[critic-only manual workflow](docs/critic-network-fallback.md) remains available.

Codex uses native compaction. A transcript-based gauge is documented in
[the proposal](codex/CONTEXT-GAUGE.md); it is **not implemented or installed**.
The Claude relay remains Claude-specific.

## PR and CI status

The shared helper prints one compact table of open PRs and their checks. Listings
default to 100 PRs and warn if more exist; use `--limit 500` to raise the limit.
Explicit PR numbers are fetched with at most four concurrent requests:

```sh
# Codex installation
PATH="${CODEX_HOME:-$HOME/.codex}/bin:$PATH" pr-status

# Claude Code installation
"${CLAUDE_HOME:-$HOME/.claude}/bin/pr-status" --failed
```

On Windows, invoke the extensionless Python helper through Python so its file
association cannot open it in an editor:

```powershell
python "$env:USERPROFILE/.codex/bin/pr-status" 4 --failed
python "$env:USERPROFILE/.claude/bin/pr-status" --failed
```

Use the corresponding `CODEX_HOME` or `CLAUDE_HOME` path for custom installs.

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
| `bin/agent-run.py`, `docs/agent-routing.md` | Restricted Windows launch, model fallback and per-role network consent |
| `bin/agent-report.py`, `bin/compare-*-readonly.py`, `benchmarks/` | Aggregate private launcher evidence and run bounded model comparisons |
| `tests/` | Bundle contracts, both installers, and relay behavior |
| `.github/workflows/ci.yml` | Python 3.11–3.13 Linux CI plus Windows/macOS 3.12, compilation, shell syntax, and tests |

## Update and verify

After `git pull`, rerun the installer for the assistant you use. Start with
`--dry-run`; review any local managed-file conflict before using `--force`.

Run the complete verification suite without touching your real assistant homes:

```sh
python3 -m unittest discover -s tests -v
```

On Windows, run `python -m unittest discover -s tests -v`. The three POSIX-shell
integration tests are skipped there and remain required in Linux CI; executable
permission-bit assertions apply only on POSIX. Python subprocess checks use the
interpreter running the suite.

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

## License

[MIT](LICENSE), copyright David Sheffer.
