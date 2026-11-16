# Route comparison protocol

The six questions in `claude-readonly.json` are a small, deterministic lookup set. They are useful for checking whether a model can find and cite current repository behavior; they are not a substitute for real user tasks or blind review.

Run the paired, read-only clients with new private output paths:

```sh
python bin/compare-claude-readonly.py --output <private-path>/claude-readonly.json
python bin/compare-codex-readonly.py --output <private-path>/codex-readonly.json
```

The Claude comparison limits each call to read tools and a USD budget. The Codex comparison uses the Windows launcher, its normal permission probes, and `--trial-model`, which accepts only models already configured for the scout role and disables automatic model fallback for that run. Both tools store answer text in the requested private file so a reviewer can inspect correctness. Do not publish those files without reviewing their contents. Literal `required_hits` are a cue for manual review, **not** a correctness grade.

Both commands exit non-zero when any run fails, returns no answer, misses a required term (every term in `required`, or at least `min_hits` of them when a task sets that), or leaves `git status --porcelain` of the target repository different from before the run; `--repo` must therefore be a git work tree.

The specified-edit fixtures in `fixtures/` were copied to separate disposable workspaces for Sonnet and Opus. Each model could edit only `solution.py` through Claude's restricted file tools; an external runner executed `python -m unittest -q`. The `summary` fixture gained missing-record cases after the first pass, so the repair measurement is reported separately. The fixture tests are intentionally small and do not measure large changes, integration, or security-sensitive edits.

## 2026-09-23 local pilot

| Comparison | Observed result | Limit |
|---|---|---|
| Claude Haiku vs Sonnet, six lookups | Haiku $0.187, median 19.0 s; Sonnet $0.294, median 16.3 s. Both had a partial error in the network-consent answer. | Same repository and questions; no blind human grader. |
| Codex Luna vs Sol, six lookups | Both completed six. Luna's relay answer cited a previous-release fixture rather than current source; Sol cited current source. Median model times were 14.0 s and 15.3 s respectively. | CLI completion is not correctness. API token prices do not establish subscription cost. |
| Claude Sonnet vs Opus, two specified edits | Original tests passed for both. Expanded invalid-record tests exposed one Sonnet error; a Sonnet repair passed. Total Sonnet cost/time including repair: about $0.106 / 42 s; Opus: about $0.230 / 51 s. | Synthetic functions, four model runs and one repair. |

## 2026-11-14 effort run

`--efforts low medium` adds a reasoning-effort axis: each task runs once per model and effort, and each record and output line carries the effort (for example `haiku@low`). Run it with no other pytest or git activity in the target repository, since files written mid-run count as a read-only failure.

```sh
python bin/compare-claude-readonly.py --models haiku sonnet --efforts low medium --output <private-path>/effort.json
```

| Model @ effort | Passed (of 6) | Total cost | Median duration |
|---|---|---|---|
| Haiku @ low | 6 | $0.020 | 15.7 s |
| Haiku @ medium | 6 | $0.019 | 16.8 s |
| Sonnet @ low | 4 | $0.242 | 15.3 s |
| Sonnet @ medium | 2 | $0.248 | 16.1 s |

Sonnet's failures were three read-only errors caused by concurrent git activity in the target repository and three missed required terms. The run supports Haiku at `effort: low` for scout lookups. It does not measure runner command execution, builds, edits, or review. A 2026-11-15 spot check (`claude -p --model haiku --effort low|high`, two runs each on one short reasoning prompt) found no consistent difference in output tokens (low 262 and 384, high 322 and 235).

A larger 2026-11-15 check ran `claude -p --model haiku --effort low|high --tools ""` from an empty directory on three short reasoning prompts with known answers (inclusion-exclusion count, 12-step modular recurrence, anagram count), four interleaved runs per prompt and effort: 24 runs, all on `claude-haiku-5-5`, 0 errors, about $0.22.

| Prompt | Correct low / high | Median output tokens low / high | Median seconds low / high |
|---|---|---|---|
| inclusion-exclusion | 4/4 / 4/4 | 390 / 341 | 16.5 / 14.4 |
| recurrence | 4/4 / 4/4 | 401 / 391 | 16.2 / 17.0 |
| anagrams | 4/4 / 4/4 | 93 / 117 | 17.5 / 15.8 |

Per-run token ranges overlapped on every prompt and high was not consistently longer, so `--effort` had no measurable effect on Haiku 5.5's accuracy, output length or latency here. Either the CLI or model ignores effort for Haiku, or the effect is below noise on prompts this short. Keeping `effort: low` on scout and runner costs nothing either way; this does not measure long, tool-using tasks.

## 2026-11-15 builder and critic effort run

`benchmarks/effort/bench.py` runs the builder role's model (`sonnet`, resolved to `claude-sonnet-5-5`) and the critic's (`claude-fable-5-2`) at `--effort low|medium|high`, interleaved, three in parallel, with a USD cap. `--check` validates the task set offline, with no model calls: each builder reference solution passes its visible and hidden tests, each stub fails them, and each seeded critic line occurs exactly once.

```sh
python benchmarks/effort/bench.py --check
python benchmarks/effort/bench.py --output <private-path>/effort.json --cap 12
python benchmarks/effort/bench.py --output <private-path>/fable.json --only builder --builder-reps 1 --builder-model claude-fable-5-2 --efforts low,medium --cap 2
```

`--builder-model` and `--critic-model` take a full versioned model id (such as `claude-opus-5-5`), which is also the id every run must be served by; aliases such as `fable` are rejected because `fable` can resolve to an older model or fall back. `--efforts` takes a comma-separated subset of `low,medium,high,xhigh,max`. A run that reports a cost which is not a finite non-negative number is labelled `bad cost` and counted as its $1.5 per-call budget, so `--cap` still stops the benchmark. A row with no cost at all (timeout, unparseable output or a crash) is marked `cost_unknown` and also counts the budget. A critic finding without a readable line number counts as an extra finding. The Opus and Fable follow-ups below ran before these flags existed, on a copy with the model and effort constants edited; the third command reproduces the Fable run.

- **Builder:** six specified edits (`ranges`, `summary`, `semver`, `csvsplit`, `ttlcache`, `ini`), three runs per effort. Each run starts from a stub in a disposable directory with only Read, Edit and Write; the harness then runs the visible tests and hidden expanded tests itself. Editing the test file or creating other files fails the run.
- **Critic:** eight small diffs, two runs per effort, read-only tools, JSON findings. Seven diffs carry one known defect: an off-by-one limit, a swallowed exception, path traversal, a check-then-open lock race, input mutation, a mutable default argument, and a positional-signature break. A run scores when a finding names the seeded line within two lines. One diff (`money`) is meant to be clean.

A first probe (two runs per arm on one builder task and one critic diff) showed that `--effort` does change Sonnet's and Fable's output, unlike Haiku: about 44% more output tokens at high for Sonnet and about 33% more for Fable. The full run had 102 runs, 0 errors, cost $4.05, and was served only by the requested models, plus the CLI's own small Haiku calls.

| Role @ effort | Correct | Median output tokens | Median seconds | Cost per run |
|---|---|---|---|---|
| builder @ low | 18/18 | 617 | 8.0 | $0.024 |
| builder @ medium | 18/18 | 739 | 8.8 | $0.027 |
| builder @ high | 18/18 | 1082 | 11.2 | $0.032 |
| critic @ low | 14/14 seeded, 1/2 clean | 332 | 7.7 | $0.051 |
| critic @ medium | 14/14 seeded, 1/2 clean | 351 | 7.7 | $0.052 |
| critic @ high | 14/14 seeded, 2/2 clean | 670 | 11.1 | $0.058 |

The `retry` diff was built as a clean case. Every critic run, at every effort, found a real defect in it: inserting `max_delay` before `sleep` breaks callers that pass `sleep` positionally. It is now relabelled as seeded, and the table counts it that way. On the clean `money` diff, the low and medium runs each reported once that `'-0'` passes the `value < 0` check. That is debatable, since negative zero is not below zero, and with two runs per arm it is noise.

After the run, a review tightened the harness: a hanging solution now scores as a failed run instead of dropping its row; any non-Haiku model other than the expected id fails the run (the run's rows were checked by hand: only claude-sonnet-5-5, claude-fable-5-2 and the CLI's Haiku calls appear); `lockfile` accepts lines 9-12; the `summary` and `semver` specs now state two behaviours their hidden tests already assumed. The table is from the run before these changes; none of them changes a recorded result. Rescoring all 102 recorded rows with the current scorer and model check gave 0 changed scores and 0 model mismatches.

Decision rule, set before the run: pin the cheapest effort that is within one task of the best arm and adds no false positives. That gives the builder `effort: low`: the same accuracy as high at about 25% lower cost and 3 s faster. For the critic the rule rejects low and medium, because each adds one debatable `money` finding, and points to `high`. With two runs per arm, though, that one-finding gap is noise. The critic therefore gets no pin and is unpinned, so it sends Fable's default effort, `high`, which was its behaviour before this run. Pinning it at `high` is still an open choice: a missed defect in a required review costs more than the $0.007 and 3 s per run that a lower effort saves. Every builder arm scored 18/18, so these tasks sit at Sonnet's ceiling. The run shows that low does not hurt small specified edits. It does not show that low is safe on large, ambiguous or multi-file changes.

Subagent transcripts record the effort that each API request sent. In transcripts from 2026-11-14 on, roles whose frontmatter pins `effort: low` (runner, scout) sent `low` once the pinned role file was installed. A reinstall can reach a running session: the builder pin was written at 11:49:06Z on 2026-11-15; in a session started before that, a builder launched about 25 s later still sent `medium`, and the next builder, 22 minutes later with no session restart, sent `low`. A builder in a new session also sent `low`. When a running session picks up a changed role file is not known, so start a new session to be sure. Unpinned roles did not send a session-wide value; each sent its model's default: `medium` on Sonnet 5.5 and Opus 5.5, and `high` on Fable 5.2. Jev routing also moves most builder calls off Sonnet: of 46 builder calls from 2026-11-14 to 2026-11-15 (UTC), 25 ran on `claude-opus-5-5`, 19 on `claude-sonnet-5-5` and 2 on `claude-fable-5-2`. The frontmatter effort still applies after Jev changes the model: after the pin, six builder calls that Jev moved to Opus all sent `low`. A builder launched with `model: fable` on 2026-11-15, after the pin, sent `low` on `claude-fable-5-2` (3 of 3 requests; Jev proposed Sonnet but kept the explicit model). A builder that Jev itself moves to Fable is not yet observed.

A follow-up run used the same six builder tasks with Opus as the builder (`claude-opus-5-5`, two runs per effort, 24 runs, $1.45, 0 errors, served only by Opus plus the CLI's Haiku calls). Low effort matched medium at 12/12 each; low had a median of 952 output tokens against 1188, 18.8 s against 21.8 s, and $0.057 against $0.064 per run. Opus at low effort is therefore measured on these tasks, with the same ceiling caveat as Sonnet.

A second follow-up ran the same six builder tasks with Fable as the builder (`claude-fable-5-2`, one run per effort, 12 runs, $0.99, 0 errors, served only by Fable plus the CLI's Haiku calls). Low effort matched medium at 6/6 each; low had a median of 1039 output tokens against 1180, about 22 s for both, and $0.078 against $0.082 per run. Fable's default effort, `high`, was not run. Fable at low effort is therefore measured on these tasks, with one run per cell and the same ceiling caveat.

A third follow-up, on 2026-11-16, tested harder builder tasks, because the six above sit at the ceiling. `--task-set hard` runs six multi-file tasks from `benchmarks/effort/hard_tasks.py`, each with two or three files to change:
- `settings_leak`: a bug report about settings shared between services.
- `cursor_pages`: offset cursors that skip or repeat items when the data changes between pages.
- `units_refactor`: move a table into a new module, add temperature conversion, and keep an existing caller working.
- `install_order`: deterministic dependency order with cycle reporting.
- `event_bus`: changing handlers while an event is being emitted.
- `money_cents`: replace float prices with integer cents.

Specs are written more like bug reports than full specifications. Hidden tests check edge cases that follow from the spec but are not spelled out in it. Each task names the files the builder may edit or create; changing any other file, including `test_solution.py`, or adding a stray file fails the run. Multi-file runs also get Glob and Grep. `--check` also validates each task's file layout.

```bash
python benchmarks/effort/bench.py --output <private-path>/hard.json --only builder --task-set hard --builder-reps 2 --efforts low,medium --cap 6
```

The Sonnet builder (`claude-sonnet-5-5`) ran each task twice per effort: 24 runs, $0.80, 0 errors, served only by Sonnet plus the CLI's Haiku calls.

| Builder @ effort | Correct | Median output tokens | Median seconds | Cost per run |
|---|---|---|---|---|
| sonnet @ low | 12/12 | 1179 | 20.0 | $0.033 |
| sonnet @ medium | 12/12 | 1258 | 20.4 | $0.034 |

These tasks also sit at the ceiling. They took about twice the output tokens of the single-file tasks at the same effort, but low still passed every hidden edge-case test. The run supports keeping the builder at `effort: low` for bounded multi-file changes of this size. It does not show where low starts to hurt; finding that would need larger changes, or specs with real gaps, than a benchmark with fixed hidden tests can grade fairly.

`claude -p --model fable` resolved to `claude-fable-5-1`, which billed about $0.18 and returned no output tokens; `claude-opus-4-8` then produced the answer. This happened in 3 of 3 calls. A critic subagent with `model: fable` served every response from `claude-fable-5-2` (7 of 7 in its transcript), so the role is unaffected in that test. Inside Claude Code the alias also reached 5.2 when Jev chose it: Jev's log shows 17 subagent calls it moved to `fable` (2 builders, 11 general-purpose, 4 scouts), and all 40 non-critic subagents with `model: fable` in local transcripts up to 2026-11-15 were served only by `claude-fable-5-2`. Fallback still happens: 3 critic transcripts with `model: fable` had part of their responses served by `claude-opus-4-8`. Benchmark scripts should pass the explicit model id.

The raw local pilot artifacts are in the user's private `~/.codex/agent-runs/` directory. Model, version, pricing, and task mix can change; repeat on a broader frozen task set before claiming general savings.

# Jev routing benchmark

`jev-routing.json` holds 30 labelled subagent tasks (10 each for sonnet, opus and fable). Run `python bin/eval-jev-routing.py` (needs `TYPESAFE_API_KEY`; `--dry-run` only validates the file). It reports two numbers:

- **accuracy**: the raw classifier choice against the expected tier.
- **applied accuracy**: the model that would actually run after `bin/jev-route.py`'s own rule. Pinned agents, answers below `min_confidence` or with an invalid confidence keep the agent's frontmatter model (or `inherit`), and a choice equal to the current tier is a same-model skip.

Every case uses a routable role. Agents in `pinned_agents` (by default `critic`, `fork`, `scout`, and `runner`) are never classified in production, so adversarial-review, lookup, and command-running scenarios use `general-purpose`.
