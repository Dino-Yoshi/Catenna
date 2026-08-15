# Catenna

Catenna is a deterministic multi-agent pipeline orchestrator. It drives
coding-agent CLIs (`codex`, `claude`, `agy`/Antigravity) through a fixed,
auditable workflow - spec, independent audit, implementation, independent
review, deterministic accept/reject - against a *driven project*: any
other repository you point it at. It exists so that agentic coding work
gets planned and checked the way a human team would, instead of one agent
improvising an entire feature in a single unsupervised shot.

Catenna is not itself the driven project. It's a standalone tool you
install once and reuse across as many projects as you like, each keeping
its own task data under a gitignored `.agent-pipeline/` directory.

## The pipeline: 9 stages, one gate

Every task moves through the same numbered stages, each producing one
artifact file under `.agent-pipeline/tasks/<task>/`:

| Stage | Artifact | What it does | Default agent |
|-------|----------|---------------|----------------|
| `00` | `00_original_request.md` | The raw task ask, seeded by a human or an overseer. | - |
| `01` | `01_requirements_packet.md` | Requirements/design packet: objective, current vs. desired behavior, constraints, acceptance criteria. | - |
| `02` | `02_technical_spec.md` | A technical specification for the change. | codex |
| `03` | `03_audit.md` | An independent audit of stage 02's spec. | codex |
| `04` | `04_final_codex_brief.md` | The final implementation brief. | codex |
| `04_gate` | `04_final_brief_audit.md` | A gate check on the brief, by an agent guaranteed *not* to be the one that wrote it - rejects and loops stage 04 back with the rejection reason inlined if the brief isn't implementable as written. | claude |
| `05` | `05_codex_implementation_report.md` | The actual code change, applied to your working tree. | codex |
| `06` | `06_manual_test_notes.md` | Manual test evidence, written by a human or auto-written by the controller when real build/test verification qualifies. | - |
| `07` | `07_diff_review.md` | An independent review of the real `git diff`, by an agent guaranteed not to be stage 05's implementer. | claude |
| `08` | `08_decision.md` | Deterministic accept / reject / needs-followup synthesis - no agent call, just a "worst wins" rule over stages 06 and 07. | - |

Stage `04_gate` is the one built-in loop: a rejected brief gets sent back
to stage `04` with the rejection feedback inlined into the retry prompt,
up to a configured number of passes, before the whole thing is treated as
blocked.

All of this is configurable per stage - which agent is primary, which
agents are fallbacks, and (since v3) per-stage model/effort overrides -
in `.agent-pipeline/config/orchestrator.json`. See "Configuration" below.

## Install

```bash
pip install catenna
```

Or, to work on Catenna itself (editable install from source):

```bash
git clone git@github.com:Dino-Yoshi/Catenna.git && cd Catenna
pip install -e .
```

Either way this installs the `catenna` console script (`pyproject.toml`);
the underlying package is `agent_pipeline`, invocable identically as
`python3 -m agent_pipeline.cli` if you ever need that form instead (some
internal verification checks shell out to it that way).

## Quick start

From the **driven project's** repository (not this one, unless you're
using Catenna to work on itself):

```bash
catenna init --codex-model <model>
```

This scaffolds `.agent-pipeline/tasks/`, `.agent-pipeline/usage/`, and
`.agent-pipeline/config/orchestrator.json` with defaults from
`agent_pipeline/config.py`.

- **Set a model for codex.** Codex has no default model
  (`agents.codex.model` is `null` out of the box) - leave it unset and
  codex invocations run without a `--model` flag at all. Claude and agy
  also default to `null`, but codex is the primary agent for the most
  invoked stages (`02`, `03`, `04`, `05`, `overseer`), so this is the one
  worth setting deliberately rather than leaving implicit. `--codex-model`
  writes it straight into the scaffolded config; without it, edit
  `agents.codex.model` in `orchestrator.json` by hand afterward. Either
  way, `pricing.codex` still needs its own rates configured separately if
  you want real cost accounting, not just usage counts.
- Set `verification.driven_project_commands` if you want Stage 6 to ever
  auto-verify (build/test commands run against *your* project, not
  Catenna's own).
- Anything else - per-stage model/effort overrides, cost-control
  downgrade eligibility, turn budgets - can stay at defaults for a first
  run.

### Plan before you run

Before pointing Catenna at real implementation work, plan it out the same
way you'd plan work for a human team: figure out the actual scope and
divide it into tasks, so the codebase scales with real requirements
instead of one task attempting to build everything at once, randomly or
obtusely. Concretely, that means writing real `00_original_request.md` /
`01_requirements_packet.md` content per task - objective, current vs.
desired behavior, constraints, acceptance criteria - not a one-line ask.
This is true whether you're seeding tasks by hand or having an overseer
pass do it: requirements and design come before implementation tasks are
handed to stage `02`, every time.

### Running a task

```bash
catenna use my-task          # or: select / set
catenna run --background     # or: --bg
catenna tail
```

`catenna use` sets the current-task pointer so subsequent commands can
omit the task argument. Run in the background from the start - it frees
your shell immediately rather than blocking on however long stages 02-05
take. `catenna tail` is an overseer's main tool for watching a background
run live from the same terminal.

For a more human-friendly, truncated view instead of a live stream, use:

```bash
catenna status my-task
catenna report my-task
```

`status` shows current state at a glance; `report` synthesizes stage
status, the Stage 8 decision, verification results, usage, and captured
reasoning traces into one readable document.

### Unblocking a stuck task

- **`awaiting_retry_approval`**: an expensive retry needs a human nod.
  Read why via `catenna status` / `catenna report`, then
  `catenna approve-retry --approval-id <id>` if it's warranted.
- **A stale lock**: confirm via `status`/`report` that it's actually
  stale, then `catenna unlock <task> --reason <reason>`.
- **Turn or attempt budget exhausted on a non-code problem** (a session
  limit, a flaky/stale review, not an actual bug): bump
  `turn_budgets.<stage>` or `stage_attempt_budget` in
  `.agent-pipeline/config/orchestrator.json`, rerun, then revert the
  bump back once the task clears it. `catenna run` is always safe to
  rerun - it resumes from whatever `reconcile_artifacts` finds valid on
  disk, so there's no special "resume" command.

## Configuration

Everything above is driven by `.agent-pipeline/config/orchestrator.json`,
merged over `agent_pipeline/config.py`'s `DEFAULT_CONFIG`. This covers the
common knobs; it is not the full reference.

## Documentation

This README covers first-run setup and the everyday commands. For
anything not covered here - the full config field reference, state
machine and troubleshooting table, cost-control/downgrade behavior, the
self-hosting overseer workflow, and architecture/module internals - see:

- [docs/USAGE.md](docs/USAGE.md) - operator guide: full command table,
  state troubleshooting, complete config reference, self-hosting workflow.
- [docs/OVERVIEW.md](docs/OVERVIEW.md) - architecture, module-by-module
  internals, and the project's changelog.
