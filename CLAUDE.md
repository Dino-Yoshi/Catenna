# Catenna — quick reference for Claude Code sessions

This repo is `agent_pipeline/`, nicknamed **Catenna**: a deterministic
multi-agent pipeline orchestrator. Full design/state-machine docs live in
`docs/OVERVIEW.md` — read that for how the pipeline actually works. This
file is just CLI ergonomics so a fresh session doesn't have to rediscover
them.

## Invoking it

Two equivalent forms, same code:

- `catenna <command> [task] [options]` — installed console-script
  (`pip install -e .`, entry point in `pyproject.toml`).
- `python3 -m agent_pipeline.cli <command> [task] [options]` — module form.
  **This must keep working.** `agent_pipeline/verification.py` shells out to
  it internally (`["-m", "agent_pipeline.cli", "mock-test"]`) — don't
  "clean up" this form away in some future pass; it's load-bearing, not
  just a legacy convenience.
- `catenna run <task> --background` — starts the normal `run` command in a
  detached child and returns after launch. The parent exit code only means
  the child process was started, not that the pipeline succeeded. Follow
  progress with `catenna tail <task>` and `catenna status <task>`.
- `catenna verify <task> --background` — starts verification in a detached
  child and returns after launch. The parent exit code only means the child
  process was started. Check the verification report and
  `.orchestrator/background_verify.log`; `catenna tail` does not cover
  verify output.

**Editable install only.** `pyproject.toml`'s setuptools config only lists
`packages = ["agent_pipeline"]` — minimal on purpose. `pip install -e .` is
fine (the source tree stays put, so `FIXTURES_ROOT`/`load_scenarios` and
`agent_pipeline/tests` keep resolving correctly relative to `__file__`). A
**non-editable** `pip install .` would silently omit top-level `fixtures/`
and `agent_pipeline/tests`, breaking the installed `catenna`'s
`mock-test`/`mock-run`/`verify` commands. Nobody has needed a non-editable
install yet (single-maintainer, dev-local tool); if that changes, package
`fixtures/` as data and re-test before relying on it.

## The `task` argument

`task` is **positional and optional** on every task-taking command except
`usage`. If you omit it, the command falls back to a persisted
"current task" pointer (`.agent-pipeline/current-task`); if neither an
explicit task nor a pointer is set, the command fails with a clear message
instead of an argparse usage dump.

- `catenna use <task>` / `catenna select <task>` / `catenna set <task>` —
  three names, identical behavior. Sets the current-task pointer. Setting
  is permissive: it does not require the task directory to already exist
  (tasks are created lazily elsewhere in this codebase) — it warns, not
  blocks, if the directory isn't there yet.
- `catenna use` (no argument) — prints whatever is currently set, or says
  nothing is set.
- `catenna tasks` / `catenna ls` — two names, identical behavior. Lists
  every task directory under `.agent-pipeline/tasks/` with its state,
  marking whichever one is current. This is the "what was I working on"
  command — useful precisely because the pointer is otherwise easy to
  forget between sessions.

`usage` is the one exception: its `--task` stays a plain optional **flag**
(not positional), and "no task given" means "show usage across *all*
tasks" — a filter default, not a target. It does **not** consult the
current-task pointer. `mock-test` never takes a task at all.

## Command names

The `pipeline-` prefix was dropped from six commands: `run`, `tail`,
`brief`, `verify`, `usage`, `report` (formerly `pipeline-run`,
`pipeline-tail`, `pipeline-brief`, `pipeline-verify`, `pipeline-usage`,
`pipeline-report`). `mock-run` / `mock-test` / `status` / `dry-run` /
`approve-retry` / `unlock` kept their names as-is — the `mock-` prefix is
deliberate, marking Catenna's own dev/debug-only path, a different
audience from day-to-day pipeline use.

This was a clean break (no deprecated aliases for the old `pipeline-*`
names) — Catenna is currently private/single-maintainer with no external
consumers. Controller function names (`pipeline_run`, `pipeline_verify`,
etc., in `controller.py`) were **not** renamed — only the CLI-facing
command strings and argument shapes changed.

## Help, completion, and color

- `catenna help` / `catenna help <command>` — git-style help, equivalent to
  `catenna --help` / `catenna <command> --help`. Works for aliases too
  (`catenna help ls` == `catenna help tasks`). An unknown command name is a
  clear error (`EXIT_BAD_INPUT`), not a stack trace.
- `catenna completion bash` — prints a bash tab-completion function,
  generated at print-time from the live `build_parser()` (command names,
  aliases, flags, and which commands take a positional task). Install with:
  `eval "$(catenna completion bash)"` (e.g. in `~/.bashrc`) so it's
  regenerated fresh each shell startup and can't go stale relative to the
  installed `catenna`. Bash only — zsh/fish are out of scope. Dynamic
  task-name completion shells out to `catenna tasks --plain`.
- `catenna tasks --plain` / `catenna ls --plain` — bare task names, one per
  line, no marker/state/color. Exists for the completion function to shell
  out to; not meant for humans.
- `agent_pipeline/color.py` — raw ANSI codes, stdlib-only (no `colorama`/
  `rich`). Respects `NO_COLOR`/`FORCE_COLOR` and checks the destination
  stream's `isatty()`, so piped/redirected output stays plain. Colors are
  targeted, not comprehensive: task/run state in `status`/`tasks`
  (running/awaiting-*=yellow, complete/passed=green,
  failed/blocked/CORRUPT=red), pass/fail in `verify`, and
  `ControllerError` messages (red, printed to stderr).

## Where things live

- `agent_pipeline/cli.py` — argparse wiring; `build_parser()`; also
  `print_help_for`/`build_completion_bash` for the `help`/`completion`
  commands.
- `agent_pipeline/controller.py` — the actual command implementations,
  including `resolve_task`/`read_current_task`/`write_current_task`/
  `use_task`/`list_tasks` for the current-task pointer.
- `agent_pipeline/color.py` — ANSI color helpers and the `STATE_COLOR` map.
- `.agent-pipeline/tasks/<task>/` — per-task state and artifacts.
- `.agent-pipeline/current-task` — the pointer file `use`/`select`/`set`
  read and write.
