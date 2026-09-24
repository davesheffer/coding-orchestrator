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

Start a new Claude Code session after installation. Roles have bounded turns,
explicit permission modes, narrow tool allowlists, and no MCP tools. Claude Code
can still apply a stronger parent permission mode, so use `/tasks` and `/status`
to confirm the effective model and settings when validating a new machine.

On Windows, use `python claude/install.py --dry-run` followed by
`python claude/install.py`. Claude's generated hooks still require a POSIX shell
and a working `python3` command (for example, through Git Bash); native PowerShell
installation alone does not verify those hooks. For a custom destination, set
`CLAUDE_CONFIG_DIR` to the same directory as `CLAUDE_HOME` when launching Claude.

#### Context relay and rollover

The relay (`relay/relay.py`) runs as `UserPromptSubmit` and `Stop` hooks. Once a
session is non-trivial (`task_shift_min_tokens`, default 30k), each prompt gets a
context gauge:

| Zone | Default threshold | What the model is told |
|---|---|---|
| GREEN | below `soft_tokens` (150k) | Work normally; roll over only if the prompt starts unrelated work |
| AMBER | `soft_tokens` (150k) | Push reading and mechanical work to subagents; roll over at the next natural boundary |
| RED | `hard_tokens` (250k) | Roll over now; the `Stop` hook blocks one stop to enforce it |

A **handoff** is a self-contained Markdown note (goal, state, decisions, files,
verified vs unverified, next step, and optionally the user's next prompt) that
the model pipes to `relay.py handoff --title "<title>"`. The relay saves it as
`relay/handoffs/<id>.md` and produces a resume prompt such as
`relay:1a2b3c4d continue "<title>" from the handoff.` Sending that prompt in a
fresh session injects the handoff so the new session continues the work.
Handoffs and per-session state older than `handoff_ttl_hours` (72) are removed
the next time a handoff is written; resuming a handoff refreshes its age.

Rollover has two modes:

- **`open`** (default): the relay asks the shared VS Code bridge
  (`bin/rollover-open.py`) to open a new Claude tab. If the bridge is missing or
  fails, it tries the editor's `vscode://anthropic.claude-code/open` URI with the
  prompt pre-filled (Claude Code's VS Code-family extension on macOS or Windows
  only). If neither launch is confirmed, it falls back to copy behaviour.
- **`copy`**: no tab or editor launch is attempted. The relay only copies the
  resume prompt to the clipboard and prints it. Start a new Claude session (a
  new tab or `/clear`) and paste it. The clipboard is `pbcopy` on macOS, `clip`
  on Windows, or the first of `wl-copy`, `xclip`, or `xsel` found on Linux. If
  none works, the relay prints `clipboard unavailable` and the prompt; the
  handoff still succeeds.

Choose the mode in any of these ways (highest precedence first):

1. `CLAUDE_RELAY_ROLLOVER=copy` or `open` in the environment.
2. `"rollover": "copy"` or `"open"` in the installed `relay/config.json`. Run
   `./install.sh --rollover copy` to set it. The flag updates only that key in
   an existing config, keeps a backup, and respects `--dry-run`.
3. Legacy `"auto_open": false` in `relay/config.json` still means `copy`.

`relay.py handoff --no-open` behaves as `copy` for a single handoff. Without
`--rollover`, the installer never changes an existing `relay/config.json`, which
also holds the zone thresholds.

Each relay measurement reads at most the last **8 MiB** of the transcript. Missing
usage or a compaction boundary without subsequent usage yields **unknown**, never
a guessed zone or forced rollover. The relay's transcript format is a heuristic;
live client validation remains necessary after client upgrades.
Handoff files and relay input/output use UTF-8, including on Windows.
Legacy handoffs can still be read using the machine's local encoding. If a
handoff was moved from a different locale and cannot be decoded, the hook reports
that it needs conversion instead of silently discarding its contents.

#### Optional: Jev integrations

`./install.sh --jev` turns on five opt-in hooks. They use TypeSafe's hosted Jev
classifier (`POST https://api.typesafe.ai/v1/systemone`) to route subagents, spot
task changes, gate risky commits, check subagent reports, and grade handoffs.
The scripts are always installed. Only `--jev` registers the hooks and sets
`jev.enabled` to `true` in the installed `relay/config.json`. Rerunning
`./install.sh` without `--jev` removes only the hooks this bundle owns and sets
`jev.enabled` back to `false`. Your own hooks and other `jev` keys stay. A
`TYPESAFE_API_KEY` in the environment alone never turns anything on.

| Feature | Runs in | Question for Jev | What happens | Sent to TypeSafe |
|---|---|---|---|---|
| `route` | `PreToolUse` `Agent\|Task` → `bin/jev-route.py` | Which tier (sonnet/opus/fable) should run this subagent task? | A confident choice that differs from the current model is applied through `updatedInput.model` | Subagent type, description, prompt (`send_prompt`, `max_prompt_chars`) |
| `shift` | `UserPromptSubmit` → `relay/relay.py prompt` | Does the new prompt continue the recent prompts / handoff goal? | Below `shift_low`: "TASK SHIFT DETECTED — roll over now". Above `shift_high`: the generic task-shift reminder is dropped | Last 5 prompts (500 chars each), handoff GOAL, new prompt (2,000 chars) |
| `risk_gate` | `PreToolUse` `Bash` → `bin/jev-guard.py gate` | Risk category (none/security/concurrency/data_loss/public_api), and whether it needs an adversarial reviewer | A risky `git commit` or `git push` with no critic run since the changed files were last modified is **denied once**. The identical retry proceeds | Operation, changed file names, diff (`send_diff`, `max_diff_chars`) |
| `report_check` | `PreToolUse` `SubagentHandback` → `jev-guard.py handback`, and `PostToolUse` `Agent\|Task\|SubagentHandback` → `jev-guard.py agent-done` | Does EVIDENCE support RESULT? Is anything material UNVERIFIED? | A weak hand-back report is **denied once**, and the subagent must verify or list the gap. An identical resend proceeds. A weak foreground report adds a "verify or escalate one tier" note for the orchestrator | The report's RESULT, EVIDENCE, CONFIDENCE and UNVERIFIED sections |
| `handoff_grade` | `relay.py handoff` | How actionable is this handoff for a fresh session (0–4)? Is NEXT STEP concrete? Do VERIFIED claims cite commands? | Below `handoff_min_score`: prints the gaps and **exits 3 without saving**. `--accept-weak` saves anyway | The handoff body (12,000 chars) |

Missing sections count as weak without asking Jev: headers must be uppercase
with a colon (`RESULT:`, `EVIDENCE:`, `CONFIDENCE:`, `UNVERIFIED:`). A
self-reported low or medium CONFIDENCE does not deny the hand-back by itself;
it only adds the foreground "verify or escalate" note. Hand-back denial is for
missing sections or a report Jev judges weak. Report checks apply to
`report_roles`. The risk gate records a critic run from when the critic was
launched, so edits made while it was still running need a fresh review; any
critic run counts, and a deleted file always needs review. For
`git add … && git commit` and `commit -a`, the gate compares the work tree
with `HEAD`, because nothing is staged when the hook runs; new untracked files
(`git ls-files --others --exclude-standard`) are included by name. Their
contents are omitted because an untracked path can be a symlink outside the
repository; review new files directly before relying on the gate's advice.

`risk_gate` is advisory, not enforcement. It can be bypassed with
`command git`, `/usr/bin/git`, `env X=1 git`, `sh -c`, a shell alias, and
similar. It also misses `git commit <pathspec>` with nothing staged, a
`"$(git push)"` inside double quotes, and `--git-dir`/`--work-tree` or
`GIT_DIR`/`GIT_WORK_TREE` pointing at another repo (these are detected, but the
gate still diffs the current directory). It understands `VAR=value git`,
`git -c k=v`, `--no-pager`, `-C <dir>` and earlier `cd <dir>` steps in the
same command, including
a subshell (`cd <dir> && git add -A && git commit`, `(cd <dir> && git commit)`),
and ignores quoted text and heredoc bodies when looking for a git commit or
push. Git calls within one gate run share a single time budget; if it runs out,
the gate allows the call. With more than 1,000 untracked files, only the first
1,000 are sent, and the change always counts as needing review.

`send_prompt` only applies to `route`. `shift` and `handoff_grade` always send
the text listed in their row above; turn those features off if you want to
avoid sending it.

Everything fails open. A missing key, a disabled feature, an HTTP error, a
malformed answer, or a timeout gives exactly the pre-Jev behaviour. Every
classifier call has a hard deadline of `min(timeout_seconds, 4)` seconds.
Denials and the handoff's exit 3 are the only ways a Jev hook changes the
outcome of a call, and each can be overridden by retrying.

Provide `TYPESAFE_API_KEY` in your shell environment, in the `"env"` block of
Claude Code's `settings.json`, or as a file path in `jev.api_key_file`. The key
is never printed or logged. Tune the integrations under the `"jev"` key of the
installed `relay/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` (`true` after `--jev`) | Set `false` to turn every feature off without reinstalling |
| `features` | all `true` | Per-feature switches: `route`, `shift`, `risk_gate`, `report_check`, `handoff_grade` |
| `api_key_file` | unset | File containing the key when `TYPESAFE_API_KEY` is not set (`~` is expanded) |
| `endpoint`, `jev_model` | TypeSafe endpoint, `"jev-latest"` | Classifier API and model. The endpoint must be `https`, or `http` only to `localhost`, `127.0.0.1`, or `::1` |
| `timeout_seconds` | `3` | Classifier deadline, capped at 4 s |
| `log` | `true` | Append one line per decision to `relay/jev-log.jsonl` |
| `min_confidence` | `0.5` | route: below this, the call is left unchanged |
| `respect_explicit_model` | `false` | route: never override a call that already sets `model` |
| `pinned_agents` | `["critic", "fork"]` | route: subagent types that are never rerouted |
| `send_prompt`, `max_prompt_chars` | `true`, `6000` | route: send the truncated task prompt, or only the type and description |
| `labels` | sonnet / opus / fable rubric | route: merged over the defaults. Set a tier to `null` to remove it. Only `sonnet`, `opus`, `haiku`, and `fable` are accepted |
| `shift_low`, `shift_high` | `0.25`, `0.75` | shift: probability-of-continuation thresholds |
| `send_diff`, `max_diff_chars` | `true`, `12000` | risk_gate: send the truncated diff, or only the operation and file names |
| `risk_min_probability` | `0.6` | risk_gate: minimum "needs review" probability to deny |
| `report_roles` | scout, runner, builder, critic | report_check: roles whose reports are checked |
| `report_min_support`, `report_max_gap` | `0.5`, `0.5` | report_check: evidence-support floor and material-gap ceiling |
| `max_report_chars` | `8000` | report_check: report text considered |
| `handoff_min_score` | `2` | handoff_grade: minimum score (0–4) to save without `--accept-weak` |

Each line of `relay/jev-log.jsonl` (mode 0600, rotated to `jev-log.jsonl.1` past
1 MiB) records the feature, the decision, the classifier's numbers, and the
latency. `route` records `desc_hash`, a short hash of the task description, so
`jev-report.py` can join routing decisions with report checks. Log lines never
contain prompt, diff or report text, task descriptions, file names, commands,
or the key. Summarize the log with:

```sh
python3 ~/.claude/bin/jev-report.py          # or --json
```

It shows routing choices per role, the confidence histogram, applied rate,
latency, and decision counts per feature. It also shows the weak-report rate
per model tier: a tier whose reports are often weak may be too small for the
tasks it gets. From the repository, `python3 bin/eval-jev-routing.py` scores
routing against the 30 labelled tasks in `benchmarks/jev-routing.json`: it
reports accuracy, a confusion matrix, confidence when right versus wrong, and
applied accuracy: the model that would actually run after `jev-route.py`'s own
rule (pinned agents, low or invalid confidence and same-tier choices keep the
current model). It makes 30 live API calls. `--dry-run` only validates the file,
and `--labels-file` tries alternative tier rubrics. The last recorded run of the
default rubrics (30/30) predates a change to some benchmark tasks' subagent
types, so rerun it before relying on that number. The rubrics were tuned on this
benchmark, so check your own log with `jev-report.py` too.

**Privacy:** with `--jev`, the text listed under "Sent to TypeSafe" goes to
TypeSafe's paid third-party API. Turn off a feature under `jev.features`, or use
`send_prompt: false` and `send_diff: false` to send less. Only while `shift` is
on, the relay keeps your last five prompts (500 chars each) and the handoff GOAL
line in its per-session state file `relay/state/<session>.json` (mode 0600),
which is removed after `handoff_ttl_hours`.

### Codex

```sh
./codex/install.sh --dry-run
./codex/install.sh
# To enable TypeSafe Jev for Codex:
./codex/install.sh --jev
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

The installer never reads `auth.json`, copies credentials, or writes to
`~/.claude`. It does not permanently change your shell's PATH. If you already
installed a helper as a symlink, the installer refuses it;
inspect and relocate that link before installing a regular copy.

`--jev` installs user-level Codex hooks in `~/.codex/hooks.json` and enables
`~/.codex/jev/config.json`. The hooks preserve unrelated entries and rerunning
the installer without `--jev` disables only this bundle's Jev hooks. Codex
requires you to review and trust these user hooks through `/hooks` before they
run; a changed hook definition needs review again. The hooks use Python 3.11+
and TypeSafe's paid Jev API. Set `TYPESAFE_API_KEY`, set `jev.api_key_file` in
the Codex Jev config, or explicitly set `jev.api_key_source` to
`"claude_settings"` to use the key already stored in Claude Code settings.
The installer never copies the key. Jev calls may send a task description,
recent prompts, a Git diff, a subagent report, or a handoff to TypeSafe;
adjust `jev.features` and the shared privacy settings in the Jev section above.

Codex Jev routes only unnamed/default subagents; named scout, runner, builder,
and critic roles keep their configured models. It checks risky `git commit` or
`git push` calls, asks weak subagent reports for one more pass, detects clear
task shifts, and grades Codex handoffs written through `rollover-open.py`.
The Git check is advisory and can be overridden by retrying the same command.
Restricted launched agents and native role profiles disable hooks, so Jev
hooks run from the main session only. Inspect decisions in
`~/.codex/jev/jev-log.jsonl`.

While `~/.codex/jev/config.json` has `jev.enabled: true`, `agent-run.py` can
give scout, runner and builder (never critic) a network profile that allows only the Jev endpoint's host
(`api.typesafe.ai` by default; the endpoint must be `https` on port 443 with a
plain DNS name, or no allowlist is used). Each backend is probed offline
first; only when that probe shows direct sockets denied is the allowlist
probed, so a backend that cannot isolate costs no extra probe. The allowlist
probe (run with `python -I`, so workspace modules cannot forge it) must show
that direct sockets are denied, the Jev host answers through the Codex proxy,
and the proxy explicitly refuses an unlisted host (`example.com`) with a CONNECT
403 or a Codex policy message. Otherwise the verified offline profile is used;
if no backend isolates, the usual approved network fallback applies. Enabling Jev is the consent:
agents may then reach that one host where they were previously offline, but
nothing wider. It does not turn hooks on inside agents. `report.json` records the
result as `jev_allowlist`. On Codex 0.155.1 under the Windows sandbox, neither
offline mode nor the domain allowlist has been observed to block the network,
so launches there still use the approved network fallback.

Claude Code needs no equivalent change. It has no network sandbox unless you
configure one, and its Jev hooks run in the main session, outside subagents.

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

Codex uses native compaction within the current session. A transcript-based
gauge is documented in [the proposal](codex/CONTEXT-GAUGE.md); it is **not
implemented or installed**. For an explicit fresh-session handoff, both clients
use the same `bin/rollover-open.py` launch protocol and the local
[VS Code handoff bridge](vscode/handoff-bridge/README.md). The Claude relay
supplies its saved handoff to that bridge; Codex supplies a self-contained
handoff when asked to roll over.

Install the bridge once in VS Code, then reload existing VS Code windows:

```sh
cd vscode/handoff-bridge
npx --yes @vscode/vsce package --no-dependencies --out handoff-bridge.vsix
code --install-extension handoff-bridge.vsix --force
```

From Codex in a VS Code project, ask it to save a handoff and open a fresh tab.
The installed global instructions tell it to pipe the state into the helper.
On Windows the direct helper is `python "$env:USERPROFILE/.codex/bin/rollover-open.py" handoff --client codex --title "task"`;
it reads the handoff from standard input. The bridge opens a new Codex tab and
copies the complete handoff with its continuation prompt; paste it and press Enter.
In rollover `open` mode, Claude's relay opens a new Claude tab with its resume
prompt prefilled; press Enter. In `copy` mode it only copies the prompt (see
[Context relay and rollover](#context-relay-and-rollover)). The helper reports
whether VS Code acknowledged opening the tab, and prints a manual continuation
prompt when it cannot confirm the launch. The bridge requires the corresponding
Claude Code or Codex VS Code extension.

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
| `bin/rollover-open.py`, `vscode/handoff-bridge/` | Shared handoff protocol and VS Code tab bridge |
| `bin/jev_client.py`, `bin/jev-route.py`, `bin/jev-guard.py`, `bin/jev-report.py`, `bin/eval-jev-routing.py` | Opt-in TypeSafe Jev hooks (routing, risk gate, report check), log report and routing eval |
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
