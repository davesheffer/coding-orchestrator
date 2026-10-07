# 🧠 Coding Orchestrator

[![CI](https://github.com/davesheffer/coding-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/davesheffer/coding-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Turn your AI coder into a team.** One smart boss plans the work and
double-checks every result. Cheaper helpers do the reading, running, and
building. Works with **Claude Code** and **Codex**.

> **Cheap hands, expensive eyes.** Helpers do the busy work; the boss keeps
> the judgment. Nothing counts as done until it's been checked.

## ✨ What you get

| | |
|---|---|
| 👥 **A team, not one bot** | Four helpers, each with one job: find code, run commands, make changes, and pick holes in the work. |
| ✅ **Proof, not promises** | Every helper reports what it did, its evidence, how sure it is, and what it didn't check. Weak reports get double-checked. |
| 🔄 **Never run out of memory** | When a chat gets too long, the boss writes a handoff note and a fresh session picks up right where it left off. |
| 📊 **Mission Control** | A live dashboard in Claude Code: what's happening now, a plain-language timeline, and which helpers are working. |
| 🛡️ **You stay in charge** | Pushing, publishing, deleting, and sending messages only happen with your OK. |

## 👥 The team

| Role | Job | Claude Code | Codex |
|---|---|---|---|
| **Boss** (orchestrator) | Plans, decides, checks the final result | Opus 5.5 | GPT-6.1 Sol |
| `scout` | Finds and reads code | Sonnet | GPT-6 Luna |
| `runner` | Runs tests and builds, reports exact results | Sonnet | GPT-6 Luna |
| `builder` | Makes a change that's already been decided | Sonnet | GPT-6.1 Sol |
| `critic` | Fresh eyes: tries to prove the work is wrong | Fable | GPT-6 Astra |

## 🚀 Quick start

```sh
git clone https://github.com/davesheffer/coding-orchestrator.git
cd coding-orchestrator
```

**Claude Code** (needs Python 3.10+):

```sh
./install.sh --dry-run   # see what would change
./install.sh             # install
```

On Windows: `python claude/install.py`.

**Codex:**

```sh
./codex/install.sh --dry-run
./codex/install.sh
```

Then start a new session. That's it. 🎉

### Handy options (Claude Code installer)

| Flag | What it does |
|---|---|
| `--rollover open` | Open the fresh session automatically when a chat gets too long (`copy` puts the prompt on your clipboard instead) |
| `--jev` | Turn on the optional Jev helpers (smarter routing and a safety check before pushes). Uses a paid third-party API |

**Mission Control** is optional: copy `mods/mission-control` to
`~/.claude/mods/mission-control` and see its [README](mods/mission-control/README.md).
Then type `/orch` in Claude Code.

## 🔄 Updating

```sh
git pull
./install.sh --jev   # leave out --jev if you don't use Jev
```

Re-running is safe: your settings (rollover included) are kept, and anything
replaced gets a backup. Only `--jev` must be passed again, or Jev turns off.

## 📚 Learn more

- **[Full reference](docs/reference.md)**: every option, the relay in depth, Jev, Codex limits, and the bundle layout
- **[How it works, for kids](docs/how-it-works-for-kids.html)** ([עברית](docs/how-it-works-for-kids.he.html))
- **[Client validation](docs/client-validation.md)**: what's been tested on which client
- **[Agent routing](docs/agent-routing.md)**: how work gets sent to the right helper

> **Experimental.** Model access and isolation depend on your client and setup.
> Check the [validation record](docs/client-validation.md) before relying on it.

## 🤝 Contributing

PRs welcome! Changes go in through pull requests with passing CI and one
approval. See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📄 License

[MIT](LICENSE) © David Sheffer
