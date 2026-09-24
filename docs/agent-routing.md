# Codex cheap-agent routing and fallbacks

The orchestrator keeps design, ambiguous debugging and final verification on
Sol/medium, escalating demanding cases to Astra. Scouts and runners start on
Luna/low; specified implementation starts on Sol/medium. Critics remain
Astra/high. Bounded routine work should be delegated without waiting for a large
context window or another user reminder.

## Install or upgrade

Fresh installs set the generic subagent default to Luna/low. Named roles keep
their own model and reasoning settings. Existing `config.toml` is byte-preserved
by default. To fill missing generic model/effort settings on an existing install:

```sh
python codex/install.py --configure-routing --dry-run
python codex/install.py --configure-routing
```

Use `--force` only after reviewing differing bundle-owned files; it creates
backups. `--configure-routing` never replaces an explicit model, effort or enabled
choice. Configurations that cannot be safely edited are refused without writes;
add the missing keys under `[agents]` manually in that case:

```toml
default_subagent_model = "gpt-6-luna"
default_subagent_reasoning_effort = "low"
```

Start a new Codex session to load changed global defaults and instructions. The
installed launcher can also be invoked from an existing session.

## Restricted Windows launcher

The bundle installs `bin/agent-run.py`. Its runtime currently supports **Windows**
and was exercised with **Codex CLI 0.155.1**. Python **3.11+** is required. Static
and simulated tests run on other platforms; they do not establish native sandbox
compatibility. Linux/macOS users retain the verified-native-role or manual
restricted-launch workflow in [the orchestrator instructions](../codex/AGENTS.md).

```powershell
python "$env:USERPROFILE/.codex/bin/agent-run.py" scout --cd C:/path/to/repo --brief C:/path/to/brief.txt
```

Use the selected `CODEX_HOME` instead of `~/.codex` when customized. Roles are
`scout`, `runner`, `builder` and `critic`. The UTF-8 brief names the task, exact
files/ownership, project constraints, acceptance command and expected output.
Omit `--brief` to read stdin. `--probe-only` checks boundaries without calling a
model. Keep the Codex home outside the delegated workspace.

Native child agents can inherit a parent's full-access overrides. When their
boundaries cannot be verified, the launcher uses a separate CLI process with a
fresh permission policy. Each launch probes workspace reads, writes inside and
outside the workspace, and an outbound TCP connection to `1.1.1.1:443` (no
application payload). It checks both available Windows sandbox modes when needed.
Scouts/critics remain read-only; runners/builders may write only in the named
workspace. Extra writable roots are not added. The launcher enumerates and checks
disabled MCP servers and external-tool features before starting the agent.
Existing project rules and hooks are preserved.

## Optional network exception

Fresh installs create this **user-owned, deny-by-default** file at
`~/.codex/agent-routing.json`:

```json
{"network_fallback_roles": []}
```

Installation never grants an exception. If the user explicitly approves and
chooses to remember the tradeoff, the orchestrator adds only the approved role
names to that user-local list. Any subset of the four roles can be approved.
Preserve unrelated policy metadata. Never distribute a personal approval with
this repository or copy an example as evidence of another user's consent.

The exception means the shell can technically access the network despite disabled
web/MCP/apps/plugins/browser/computer/image/nested-agent tools. Agents are instructed
to make no external requests; this is **not network isolation**. File limits remain
mandatory. The launcher freshly tries isolated backends first on every invocation
and rechecks file limits before using the authorized fallback. It never changes
firewall settings or disables the filesystem sandbox.

With Codex Jev enabled, scout, runner and builder first try a profile that
allows only the Jev host; it counts as isolation only when the probe shows
direct sockets denied, the Jev host reachable and an unlisted host refused by
the proxy. See the Codex section of the README.

Removing a role revokes its exception; an empty list refuses fallback for all
roles. `--no-network-fallback` requires isolation for one invocation even if a
saved approval exists. Missing policies deny fallback; malformed, duplicate-role
and symlink policies are refused. The installer preserves existing policy bytes,
including approvals and refusals, even with `--force`.

Legacy [critic-only instruction-block choices](critic-network-fallback.md) remain
preserved and available to the manual workflow. They are not automatically
converted into JSON approval. Reconcile any conflicting records with the user's
latest explicit answer before saving a launcher policy. A critic-only approval
never grants exceptions to the other roles.

## Model fallback and results

| Role | Ordered model attempts |
|---|---|
| Scout / runner | Luna/low -> Sol/low |
| Builder | Sol/medium |
| Critic | Astra/high only |

Only recognized model-unavailable errors before any work can advance the chain.
The launcher does not retry after tool/response items, partial edits, failed tests,
permission failures, authentication/rate-limit errors, malformed logs or unknown
errors. Those cases return to the main session for assessment. It never silently
upgrades routine work to Astra. A customized primary role model that disagrees
with the launcher policy is refused; reconcile the policy or use verified native
delegation instead of assuming an account has access to these model names.

`~/.codex/agent-runs/<timestamp-role-id>/` retains `report.json`, event logs, stderr
and the final agent response. Reports include permission probes, disabled tools,
requested and observed model/effort, thread IDs, CLI exits, preflight/attempt timing,
and turn token usage when available. Model identity comes
from the tested CLI's local thread database; missing or changed schemas produce
an unverified result, not a guessed model identity. These artifacts are private
local evidence and should not be committed or uploaded by default.

Use `python bin/agent-report.py --workspace C:/path/to/repo` to aggregate local
reports without printing task text. For a controlled scout comparison,
`--trial-model gpt-6-luna` or `--trial-model gpt-6-sol` selects exactly one
configured candidate and disables automatic model fallback for that run. It
does not change the permission checks. See `benchmarks/README.md` for the
read-only paired protocol and its limitations.

CLI exit zero means the agent finished, not that its task passed. Inspect RESULT,
actual acceptance-command exits and builder diffs. Evidence of a successful
network-available fallback does not turn a failed isolation probe into a pass.

## Validation

Run `python -m unittest discover -s tests -v` for installer, routing and simulated
launch coverage. Tests inject unavailable-model errors and partial/corrupt event
logs; they do not cause real service outages. Follow the separate
[native acceptance checks](client-validation.md) for actual model and sandbox
behavior. The local Windows prototype ran Luna scouts/runners, a Terra builder
and Astra critics; repository changes need their own validation record.
