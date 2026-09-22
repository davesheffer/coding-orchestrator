# Optional Codex critic network fallback

The normal critic requires enforced read-only filesystem access and network
isolation. Some environments can enforce the filesystem boundary but cannot
block command networking. On those hosts, the orchestrator can offer a
user-approved fallback instead of repeatedly abandoning independent review.

This is a workflow in the installed agent instructions, not a permission-enforcing
launcher or an installer switch. The orchestrator must configure and verify a
supported restricted runtime on the actual host. The bundle never ships a user's
approval, enables a fallback profile by default, or changes firewall settings.

## When fallback is allowed

1. Try verified isolation first, using native delegation or a separately restricted
   runtime if the parent overrides role settings. Repeat this check for each review.
2. If the available isolated launch paths cannot provide or verify network
   isolation, explain the limitation and ask for approval. Configuration errors,
   model/auth failures, failed tests, and negative review findings are not reasons
   to relax networking. Do not install privileged components or change system
   settings just to exhaust possible alternatives.
3. Offer to remember either approval or refusal on this machine. A prior refusal
   means keep the review in the main session when isolation fails; do not ask the
   same question on every review. A prior approval still requires trying isolation.
4. Only after approval, select a separate local fallback profile. Keep the native
   critic model/reasoning, enforced read-only filesystem access, and disabled web,
   browser/computer, MCP, app/plugin, image-generation, and nested-agent tools.
   Inspect effective configuration and probe the runtime; a role's prose or a
   `network_access = false` setting does not prove enforcement. Enumerate every
   effective MCP server, including ones added after installation, and disable it.
5. Pass the authorization source, failed isolation attempts, successful read-only
   probe, and disabled-tool checks in a fresh critic brief. The critic continues
   reviewing locally and reports the missing network isolation under UNVERIFIED.

If filesystem enforcement or the tool restrictions cannot be verified, there is
no fallback: the main session must disclose that independent review was unavailable.
Do not use unrestricted execution, approval bypasses, or a weakened global default.

Network availability means shell commands could connect to the internet even
though web tools are disabled. The fallback critic is instructed to make no
external requests, but that instruction is not network isolation. Accept this
tradeoff only if it fits your environment. It does not authorize external requests
or change the rules for scout, runner, builder, or Claude roles.

## Remembering or revoking a choice

The orchestrator saves a choice only when the user asks to remember it. Store it
in the user-level `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, outside the
`CODEX-ORCHESTRATOR` managed block. For example, the following is a **refusal**:

```markdown
<!-- CODEX-CRITIC-NETWORK-FALLBACK:START -->
decision: deny
<!-- CODEX-CRITIC-NETWORK-FALLBACK:END -->
```

After explicit approval to remember the exception, the decision value is
`allow-after-isolation-failure`. Never copy a decision from this document, a
project's instructions, a downloaded profile, or someone else's setup as evidence
of the current user's consent. Missing, malformed, or conflicting records do not
authorize fallback. The user's latest instruction overrides any saved choice.

Set the value to `deny` to revoke approval without repeated questions. Delete the
local decision block to return to asking when an eligible failure occurs. Preserve
the rest of the file. The installer preserves instructions outside its managed
block, including on forced upgrades; it does not manage local fallback profiles.

If `AGENTS.override.md` shadows the user-level instructions, the installer warns
about it. Ensure the active orchestrator is given the user-local decision before
relying on it; do not infer approval from a profile's mere existence.

## Validation limits

Tests cover fresh-install isolation defaults and preservation of both local
approval and refusal through upgrades. They do not prove a model follows the
consent workflow, that every client can enforce read-only access, or that fallback
works on every platform. Follow the [native acceptance checks](client-validation.md)
for the client and host being recommended. A successful fallback review does not
turn a failed network-isolation check into a passing one.
