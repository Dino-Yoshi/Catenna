# Agent-pipeline redesign — phase tracker

Full plan: see the plan-mode conversation that produced this redesign, or the
handoff docs below for each completed phase's rationale. A fresh session
should read this file first, then the most recent
`handoffs/phase-N-handoff.md`, before continuing any work here.

**Relocation note (2026-08-05):** Phases 0-5 were completed while this tool
lived inside `immersive-enchanting-1122` at `tools/agent_pipeline/`; it has
since moved to this standalone repo. See [OVERVIEW.md](OVERVIEW.md) for
details. Path references in the phases/handoffs below reflect the old
in-repo layout.

| Phase | Goal                                                              | Status       | Handoff |
|-------|--------------------------------------------------------------------|--------------|---------|
| 0     | Docs & hygiene baseline                                            | done         | [phase-0-handoff.md](handoffs/phase-0-handoff.md) |
| 1     | Live visibility into running agents (streaming, `pipeline-tail`, `pipeline-brief`) | done         | [phase-1-handoff.md](handoffs/phase-1-handoff.md) |
| 2     | Expanded test harness + `verification.py` (build/test report, test-coverage-delta signal) | done         | [phase-2-handoff.md](handoffs/phase-2-handoff.md) |
| 3     | Automated overseer-driven review & decision, absorbing legacy Stages 6-8 | done         | [phase-3-handoff.md](handoffs/phase-3-handoff.md) |
| 4     | Smart agent/model routing + usage awareness                        | done         | [phase-4-handoff.md](handoffs/phase-4-handoff.md) |
| 5     | Legible reporting + "peer into thinking" (reasoning trace capture) | done         | [phase-5-handoff.md](handoffs/phase-5-handoff.md) |

## Binding conventions (apply to every phase)

- **Single entry point**: every new CLI subcommand ships with a matching
  `Makefile.orchestrator` target in the same phase. Never leave a capability
  reachable only via raw `python3 -m tools.agent_pipeline.cli ...`. Update the
  command table in [OVERVIEW.md](OVERVIEW.md) when adding one.
- **Test-explicit**: pipeline changes that add behavior add tests for that
  behavior in the same phase. `python3 -m unittest discover -s
  tools/agent_pipeline/tests` must stay green throughout.
- **No retroactive rewrites**: `.agent-pipeline/tasks/` has ~60 real task
  directories (36 with live `.orchestrator/` state) that predate this
  redesign. New auto-fill/auto-chain logic must defer to artifacts that
  already exist and are valid (via `reconcile_artifacts`/
  `contiguous_completed`) rather than overwrite them.
- **Handoff docs are not optional**: each phase ends by writing
  `handoffs/phase-N-handoff.md` (what changed, why, exact verification
  commands, what's deliberately deferred, gotchas for the next session) and
  updating this table's status column plus [OVERVIEW.md](OVERVIEW.md).
