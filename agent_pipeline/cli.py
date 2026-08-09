"""Command line interface for the mock pipeline orchestrator."""

from __future__ import print_function

import argparse
import os
import shlex
import sys

from . import color, controller


def resolved(args):
    task, used_default = controller.resolve_task(args.task)
    if used_default:
        print(color.dim("(using current task: %s)" % task, sys.stderr), file=sys.stderr)
    return task


def _prog_name():
    if os.path.basename(sys.argv[0]) == "catenna":
        return "catenna"
    return "python3 -m agent_pipeline.cli"


def run_command(args):
    if args.background:
        return controller.pipeline_run_background(resolved(args), args.allow_dirty)
    return controller.pipeline_run(resolved(args), args.allow_dirty)


def verify_command(args):
    if args.background:
        return controller.pipeline_verify_background(resolved(args), args.build)
    return controller.pipeline_verify(resolved(args), args.build)


def build_parser():
    parser = argparse.ArgumentParser(prog=_prog_name())
    sub = parser.add_subparsers(dest="command")
    add_task(sub.add_parser("status", help="show controller status")).set_defaults(func=lambda a: controller.status(resolved(a)))
    add_task(sub.add_parser("dry-run", help="show resumable work without mutating state")).set_defaults(func=lambda a: controller.dry_run(resolved(a)))
    sub.add_parser("mock-test", help="run isolated deterministic mock scenarios").set_defaults(func=lambda a: controller.mock_test())
    mock_run = add_task(sub.add_parser("mock-run", help="run one deterministic mock scenario"))
    mock_run.add_argument("--scenario", required=True)
    mock_run.set_defaults(func=lambda a: controller.mock_run(resolved(a), a.scenario))
    run = add_task(sub.add_parser("run", help="run/resume real Stage 2-5 pipeline"))
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument("--background", action="store_true", help="launch and return immediately")
    run.set_defaults(func=run_command)
    approve = add_task(sub.add_parser("approve-retry", help="approve one pending expensive retry"))
    approve.add_argument("--approval-id", required=True)
    approve.set_defaults(func=lambda a: controller.approve_retry(resolved(a), a.approval_id))
    unlock = add_task(sub.add_parser("unlock", help="explicitly remove an orchestrator lock"))
    unlock.add_argument("--reason", required=True)
    unlock.set_defaults(func=lambda a: controller.unlock(resolved(a), a.reason))
    tail = add_task(sub.add_parser("tail", help="live-tail the current/most recent agent run"))
    tail.add_argument("--stage")
    tail.add_argument("--run-id")
    tail.add_argument("--verbose", action="store_true", help="print full event text without display truncation")
    tail.set_defaults(func=lambda a: controller.pipeline_tail(resolved(a), a.stage, a.run_id, verbose=a.verbose))
    brief = add_task(sub.add_parser("brief", help="print a compact summary of a run"))
    brief.add_argument("--stage")
    brief.add_argument("--run-id")
    brief.add_argument("--verbose", action="store_true", help="print full reasoning and final text excerpts")
    brief.set_defaults(func=lambda a: controller.pipeline_brief(resolved(a), a.stage, a.run_id, verbose=a.verbose))
    verify = add_task(sub.add_parser("verify", help="run build/test verification and write a structured report"))
    verify.add_argument("--build", action="store_true", help="also run ./gradlew build, not just compileJava")
    verify.add_argument("--background", action="store_true", help="launch and return immediately")
    verify.set_defaults(func=verify_command)
    usage_cmd = sub.add_parser("usage", help="print a usage/cost summary from the cross-task ledger")
    usage_cmd.add_argument("--task", help="filter to one task (default: all tasks)")
    usage_cmd.add_argument("--agent")
    usage_cmd.add_argument("--since-hours", type=float)
    usage_cmd.set_defaults(func=lambda a: controller.pipeline_usage(a.task, a.agent, a.since_hours))
    report = add_task(sub.add_parser("report", help="print a legible per-task report (stages, decision, verification, usage, reasoning traces)"))
    report.set_defaults(func=lambda a: controller.pipeline_report(resolved(a)))
    use = sub.add_parser("use", aliases=["select", "set"], help="set (or show) the current-task pointer")
    use.add_argument("task", nargs="?")
    use.set_defaults(func=lambda a: controller.use_task(a.task))
    tasks_cmd = sub.add_parser("tasks", aliases=["ls"], help="list task directories and their state")
    tasks_cmd.add_argument("--plain", action="store_true", help="print bare task names only, one per line")
    tasks_cmd.set_defaults(func=lambda a: controller.list_tasks(plain=a.plain))

    help_parser = sub.add_parser("help", help="show help for catenna or a specific command")
    help_parser.add_argument("command", nargs="?")
    help_parser.set_defaults(func=lambda a: print_help_for(parser, sub, a.command))

    completion_parser = sub.add_parser("completion", help="print a shell completion script")
    completion_parser.add_argument("shell", choices=["bash"])
    completion_parser.set_defaults(func=lambda a: print_completion(sub))
    return parser


def print_help_for(parser, sub, command):
    if command is None:
        parser.print_help()
        return controller.EXIT_SUCCESS
    target = sub.choices.get(command)
    if target is None:
        print("unknown command: %s" % command)
        print("available commands: " + ", ".join(sorted(sub.choices)))
        return controller.EXIT_BAD_INPUT
    target.print_help()
    return controller.EXIT_SUCCESS


def print_completion(sub):
    print(build_completion_bash(sub))
    return controller.EXIT_SUCCESS


def _subparser_metadata(subparser):
    options = []
    value_options = []
    has_task = False
    for action in subparser._actions:
        if action.option_strings:
            if "-h" in action.option_strings or "--help" in action.option_strings:
                continue
            options.extend(action.option_strings)
            if action.nargs != 0:
                value_options.extend(action.option_strings)
        elif action.dest == "task":
            has_task = True
    return {
        "options": sorted(set(options)),
        "value_options": sorted(set(value_options)),
        "has_task": has_task,
    }


def build_completion_bash(sub):
    command_names = sorted(sub.choices)
    metadata_by_id = {}
    metadata_by_name = {}
    for name, subparser in sub.choices.items():
        if id(subparser) not in metadata_by_id:
            metadata_by_id[id(subparser)] = _subparser_metadata(subparser)
        metadata_by_name[name] = metadata_by_id[id(subparser)]

    lines = []
    lines.append("# catenna bash completion")
    lines.append("# install with:")
    lines.append('#   eval "$(catenna completion bash)"')
    lines.append("# (add that line to ~/.bashrc to load it on every shell startup)")
    lines.append("_catenna_complete() {")
    lines.append("    local cur prev cmd")
    lines.append('    cur="${COMP_WORDS[COMP_CWORD]}"')
    lines.append("    COMPREPLY=()")
    lines.append("")
    lines.append("    local commands=(%s)" % " ".join(shlex.quote(n) for n in command_names))
    lines.append("")
    lines.append('    if [ "$COMP_CWORD" -eq 1 ]; then')
    lines.append('        COMPREPLY=( $(compgen -W "${commands[*]}" -- "$cur") )')
    lines.append("        return 0")
    lines.append("    fi")
    lines.append("")
    lines.append('    cmd="${COMP_WORDS[1]}"')
    lines.append("")
    lines.append('    if [ "$cmd" = "completion" ] && [ "$COMP_CWORD" -eq 2 ]; then')
    lines.append('        COMPREPLY=( $(compgen -W "bash" -- "$cur") )')
    lines.append("        return 0")
    lines.append("    fi")
    lines.append("")
    lines.append('    if [ "$cmd" = "help" ] && [ "$COMP_CWORD" -eq 2 ]; then')
    lines.append('        COMPREPLY=( $(compgen -W "${commands[*]}" -- "$cur") )')
    lines.append("        return 0")
    lines.append("    fi")
    lines.append("")
    lines.append('    prev="${COMP_WORDS[COMP_CWORD-1]}"')
    lines.append("")
    lines.append('    case "$cmd" in')
    for name in command_names:
        meta = metadata_by_name[name]
        lines.append("        %s)" % shlex.quote(name))
        if meta["value_options"]:
            pattern = "|".join(shlex.quote(v) for v in meta["value_options"])
            lines.append('            case "$prev" in')
            lines.append("                %s)" % pattern)
            lines.append("                    return 0")
            lines.append("                    ;;")
            lines.append("            esac")
        if meta["options"]:
            opts = " ".join(shlex.quote(v) for v in meta["options"])
            lines.append('            if [[ "$cur" == -* ]]; then')
            lines.append("                COMPREPLY=( $(compgen -W %s -- \"$cur\") )" % shlex.quote(opts))
            lines.append("                return 0")
            lines.append("            fi")
        if meta["has_task"]:
            lines.append('            COMPREPLY=( $(compgen -W "$(catenna tasks --plain 2>/dev/null)" -- "$cur") )')
            lines.append("            return 0")
        lines.append("            ;;")
    lines.append("    esac")
    lines.append("}")
    lines.append("complete -F _catenna_complete catenna")
    return "\n".join(lines)


def add_task(parser):
    parser.add_argument("task", nargs="?")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except controller.ControllerError as exc:
        print(color.red(str(exc), sys.stderr), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
