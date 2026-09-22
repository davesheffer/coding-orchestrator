# Validate the Codex setup

Install with `./codex/install.sh` first, then start a fresh Codex session in your
working repository. Paste this brief to validate that host's behavior:

> Read the installed global AGENTS.md and native scout role. Confirm the host
> exposes the custom role and inspect effective permissions before delegation.
> Use scout to report this repository's short Git HEAD SHA. Return its raw
> RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED block and report the actual model,
> reasoning effort, and sandbox evidence available to you. Do not edit files,
> use network tools, or infer enforced isolation from the persona's wording.
> If parent overrides prevent read-only/no-network isolation, use the separately
> restricted CLI route documented in AGENTS.md; if it is unavailable, stop and
> explain the limitation. Never invent a role-selection parameter.

For model availability changes, inspect your picker and the current official
[Codex model documentation](https://learn.chatgpt.com/docs/models) before updating
the four TOML files. Preserve the report formats and persona rules. There is no
need to retranslate the Claude files or invent a parallel Markdown role system.

Do not copy `auth.json`, tokens, personal config, or project-managed configuration
into this repository. Installing the bundle does not authorize installing the
optional context gauge; see [its proposal](CONTEXT-GAUGE.md).
