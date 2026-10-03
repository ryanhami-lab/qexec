"""The ``qexec`` command-line entry point (``[project.scripts] qexec = qexec.cli.main:main``).

Pure :mod:`argparse` (no third-party dependency). Builds the parser, dispatches to a command
implementation, and maps exceptions to process exit codes:

* ``0`` -- success
* ``2`` -- usage error (bad/missing argument, invalid value) -> :class:`CommandUsageError`
* ``1`` -- runtime failure during execution -> :class:`CommandError` / unexpected exception

All data handled by the CLI is SYNTHETIC; no command performs any network access.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from qexec.cli.commands import CommandError, CommandUsageError
from qexec.cli.commands.analysis import run_analysis_report
from qexec.cli.commands.demo import run_demo
from qexec.cli.commands.experiment import run_experiment_run
from qexec.cli.commands.replay import run_replay_validate
from qexec.cli.commands.synth import run_synth
from qexec.cli.commands.tasks import run_tasks_build
from qexec.cli.commands.trace import run_trace_task

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

_Handler = Callable[[argparse.Namespace], int]

_DESCRIPTION = (
    "QExec: queue-aware execution research replay engine (SYNTHETIC-data build). "
    "All data is synthetic software validation, never a market finding. No network access."
)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``qexec`` argument parser with all subcommands."""
    parser = argparse.ArgumentParser(prog="qexec", description=_DESCRIPTION)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    # synth
    p_synth = sub.add_parser("synth", help="generate synthetic sessions (SYNTHETIC data)")
    p_synth.add_argument("--out", required=True, help="output directory for the sessions")
    p_synth.add_argument("--sessions", type=int, required=True, help="number of sessions (>= 1)")
    p_synth.add_argument("--duration", type=int, required=True, help="session duration in seconds")
    p_synth.add_argument("--seed", type=int, required=True, help="base seed (seed+i per session)")
    p_synth.set_defaults(_handler=run_synth)

    # replay <validate>
    p_replay = sub.add_parser("replay", help="replay-engine utilities")
    replay_sub = p_replay.add_subparsers(dest="replay_command", metavar="SUBCOMMAND", required=True)
    p_replay_validate = replay_sub.add_parser(
        "validate", help="verify checksums and replay a session through ReferenceBook"
    )
    p_replay_validate.add_argument("--session", required=True, help="session directory to validate")
    p_replay_validate.set_defaults(_handler=run_replay_validate)

    # tasks <build>
    p_tasks = sub.add_parser("tasks", help="task-manifest utilities")
    tasks_sub = p_tasks.add_subparsers(dest="tasks_command", metavar="SUBCOMMAND", required=True)
    p_tasks_build = tasks_sub.add_parser(
        "build", help="build the policy-independent task manifest for a session"
    )
    p_tasks_build.add_argument("--session", required=True, help="session directory")
    p_tasks_build.add_argument(
        "--horizon-ms", type=int, default=None, help="task horizon in milliseconds (default 1000)"
    )
    p_tasks_build.set_defaults(_handler=run_tasks_build)

    # experiment <run>
    p_experiment = sub.add_parser("experiment", help="research-study utilities")
    exp_sub = p_experiment.add_subparsers(
        dest="experiment_command", metavar="SUBCOMMAND", required=True
    )
    p_experiment_run = exp_sub.add_parser("run", help="run the full research study (run_study)")
    p_experiment_run.add_argument("--out", required=True, help="study output directory")
    p_experiment_run.add_argument("--quick", action="store_true", help="use the quick preset")
    p_experiment_run.add_argument("--config", default=None, help="JSON StudyConfig overrides file")
    p_experiment_run.add_argument(
        "--sessions", type=int, default=None, help="override n_sessions (>= 4; at least 2 train)"
    )
    p_experiment_run.add_argument(
        "--workers", type=int, default=None, help="override max_workers (>= 1)"
    )
    p_experiment_run.set_defaults(_handler=run_experiment_run)

    # demo
    p_demo = sub.add_parser("demo", help="end-to-end synthetic study + report (CI smoke)")
    p_demo.add_argument("--out", required=True, help="demo output directory")
    p_demo.add_argument("--sessions", type=int, required=True, help="number of sessions")
    p_demo.add_argument("--quick", action="store_true", help="use the quick preset")
    p_demo.set_defaults(_handler=run_demo)

    # trace <task>
    p_trace = sub.add_parser("trace", help="causal-trace utilities")
    trace_sub = p_trace.add_subparsers(dest="trace_command", metavar="SUBCOMMAND", required=True)
    p_trace_task = trace_sub.add_parser(
        "task", help="print the causal trace of one (task, policy) world"
    )
    p_trace_task.add_argument(
        "--session", default=None, help="session directory (baselines B0/B1 only)"
    )
    p_trace_task.add_argument(
        "--study",
        default=None,
        help="finished study dir (loads frozen artifacts; traces B2/B3/B3_NO_QUEUE too)",
    )
    p_trace_task.add_argument("--task-id", required=True, help="task id to trace")
    p_trace_task.add_argument("--policy", default="B1", help="policy to trace")
    p_trace_task.add_argument("--scenario", default="L1", help="latency scenario (default L1)")
    p_trace_task.add_argument(
        "--horizon-ms", type=int, default=None, help="task horizon in milliseconds (default 1000)"
    )
    p_trace_task.set_defaults(_handler=run_trace_task)

    # analysis <report>
    p_analysis = sub.add_parser("analysis", help="analysis / reporting utilities")
    analysis_sub = p_analysis.add_subparsers(
        dest="analysis_command", metavar="SUBCOMMAND", required=True
    )
    p_analysis_report = analysis_sub.add_parser(
        "report", help="render the Markdown research report for a finished study"
    )
    p_analysis_report.add_argument("--study", required=True, help="study output directory")
    p_analysis_report.set_defaults(_handler=run_analysis_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (also usable via ``sys.exit(main())``)."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits with code 2 on usage errors; normalise to our usage code.
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return code if code is not None else EXIT_OK

    handler: _Handler = args._handler  # set by set_defaults on each subparser
    try:
        return handler(args)
    except CommandUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:  # pragma: no cover - interactive interruption
        print("interrupted", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
