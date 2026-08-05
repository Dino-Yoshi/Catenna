"""Command line interface for the mock pipeline orchestrator."""

from __future__ import print_function

import argparse
import sys

from . import controller


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m tools.agent_pipeline.cli")
    sub = parser.add_subparsers(dest="command")
    add_task(sub.add_parser("status", help="show controller status")).set_defaults(func=lambda a: controller.status(a.task))
    add_task(sub.add_parser("dry-run", help="show resumable work without mutating state")).set_defaults(func=lambda a: controller.dry_run(a.task))
    sub.add_parser("mock-test", help="run isolated deterministic mock scenarios").set_defaults(func=lambda a: controller.mock_test())
    mock_run = add_task(sub.add_parser("mock-run", help="run one deterministic mock scenario"))
    mock_run.add_argument("--scenario", required=True)
    mock_run.set_defaults(func=lambda a: controller.mock_run(a.task, a.scenario))
    pipeline_run = add_task(sub.add_parser("pipeline-run", help="run/resume real Stage 2-5 pipeline"))
    pipeline_run.add_argument("--allow-dirty", action="store_true")
    pipeline_run.set_defaults(func=lambda a: controller.pipeline_run(a.task, a.allow_dirty))
    approve = add_task(sub.add_parser("approve-retry", help="approve one pending expensive retry"))
    approve.add_argument("--approval-id", required=True)
    approve.set_defaults(func=lambda a: controller.approve_retry(a.task, a.approval_id))
    unlock = add_task(sub.add_parser("unlock", help="explicitly remove an orchestrator lock"))
    unlock.add_argument("--reason", required=True)
    unlock.set_defaults(func=lambda a: controller.unlock(a.task, a.reason))
    tail = add_task(sub.add_parser("pipeline-tail", help="live-tail the current/most recent agent run"))
    tail.add_argument("--stage")
    tail.add_argument("--run-id")
    tail.set_defaults(func=lambda a: controller.pipeline_tail(a.task, a.stage, a.run_id))
    brief = add_task(sub.add_parser("pipeline-brief", help="print a compact summary of a run"))
    brief.add_argument("--stage")
    brief.add_argument("--run-id")
    brief.set_defaults(func=lambda a: controller.pipeline_brief(a.task, a.stage, a.run_id))
    verify = add_task(sub.add_parser("pipeline-verify", help="run build/test verification and write a structured report"))
    verify.add_argument("--build", action="store_true", help="also run ./gradlew build, not just compileJava")
    verify.set_defaults(func=lambda a: controller.pipeline_verify(a.task, a.build))
    usage_cmd = sub.add_parser("pipeline-usage", help="print a usage/cost summary from the cross-task ledger")
    usage_cmd.add_argument("--task")
    usage_cmd.add_argument("--agent")
    usage_cmd.add_argument("--since-hours", type=float)
    usage_cmd.set_defaults(func=lambda a: controller.pipeline_usage(a.task, a.agent, a.since_hours))
    report = add_task(sub.add_parser("pipeline-report", help="print a legible per-task report (stages, decision, verification, usage, reasoning traces)"))
    report.set_defaults(func=lambda a: controller.pipeline_report(a.task))
    return parser


def add_task(parser):
    parser.add_argument("--task", required=True)
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
