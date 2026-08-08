"""Command line interface for the mock pipeline orchestrator."""

from __future__ import print_function

import argparse
import sys

from . import controller


def resolved(args):
    task, used_default = controller.resolve_task(args.task)
    if used_default:
        print("(using current task: %s)" % task, file=sys.stderr)
    return task


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m agent_pipeline.cli")
    sub = parser.add_subparsers(dest="command")
    add_task(sub.add_parser("status", help="show controller status")).set_defaults(func=lambda a: controller.status(resolved(a)))
    add_task(sub.add_parser("dry-run", help="show resumable work without mutating state")).set_defaults(func=lambda a: controller.dry_run(resolved(a)))
    sub.add_parser("mock-test", help="run isolated deterministic mock scenarios").set_defaults(func=lambda a: controller.mock_test())
    mock_run = add_task(sub.add_parser("mock-run", help="run one deterministic mock scenario"))
    mock_run.add_argument("--scenario", required=True)
    mock_run.set_defaults(func=lambda a: controller.mock_run(resolved(a), a.scenario))
    run = add_task(sub.add_parser("run", help="run/resume real Stage 2-5 pipeline"))
    run.add_argument("--allow-dirty", action="store_true")
    run.set_defaults(func=lambda a: controller.pipeline_run(resolved(a), a.allow_dirty))
    approve = add_task(sub.add_parser("approve-retry", help="approve one pending expensive retry"))
    approve.add_argument("--approval-id", required=True)
    approve.set_defaults(func=lambda a: controller.approve_retry(resolved(a), a.approval_id))
    unlock = add_task(sub.add_parser("unlock", help="explicitly remove an orchestrator lock"))
    unlock.add_argument("--reason", required=True)
    unlock.set_defaults(func=lambda a: controller.unlock(resolved(a), a.reason))
    tail = add_task(sub.add_parser("tail", help="live-tail the current/most recent agent run"))
    tail.add_argument("--stage")
    tail.add_argument("--run-id")
    tail.set_defaults(func=lambda a: controller.pipeline_tail(resolved(a), a.stage, a.run_id))
    brief = add_task(sub.add_parser("brief", help="print a compact summary of a run"))
    brief.add_argument("--stage")
    brief.add_argument("--run-id")
    brief.set_defaults(func=lambda a: controller.pipeline_brief(resolved(a), a.stage, a.run_id))
    verify = add_task(sub.add_parser("verify", help="run build/test verification and write a structured report"))
    verify.add_argument("--build", action="store_true", help="also run ./gradlew build, not just compileJava")
    verify.set_defaults(func=lambda a: controller.pipeline_verify(resolved(a), a.build))
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
    sub.add_parser("tasks", aliases=["ls"], help="list task directories and their state").set_defaults(func=lambda a: controller.list_tasks())
    return parser


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
        print(str(exc))
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
