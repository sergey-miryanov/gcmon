"""Run command implementation."""

import argparse
import logging
import sys
from argparse import Namespace

from gcmon.cli.commands.monitoring_base import run_monitoring_loop
from gcmon.cli.commands.monitoring_options import add_monitoring_options, get_monitoring_options
from gcmon.cli.shared.parser_factory import ParserFactory
from gcmon.monitoring.child_process_runner import ChildProcessRunner
from gcmon.monitoring.target_process import ProcessFactory
from gcmon.monitoring.wait_policy import StartupTimeoutPolicy

logger = logging.getLogger("gcmon")


def add_parser(parser_factory: ParserFactory) -> argparse.ArgumentParser:
    """Add the 'run' subparser and return it."""
    parser = parser_factory(
        "run",
        help="Run a Python script/module with GC monitoring",
        description="Run a Python script or module with GC monitoring enabled. "
        "All arguments after -m/--module or -s/--script are passed verbatim to the target.",
    )
    # Target specification: -m module OR script path
    # Both are optional in argparse, validation happens in cmd_run
    # Script arguments are captured via parse_known_args in main()
    parser.add_argument(
        "-m",
        "--module",
        dest="module_name",
        default=None,
        help="Module name to run (like python -m)",
    )
    parser.add_argument(
        "-s",
        "--script",
        dest="script",
        default=None,
        help="Script path to run",
    )
    # Monitoring options (same as monitor command)
    add_monitoring_options(parser)
    parser.set_defaults(func=cmd_run)
    # Note: Script arguments (everything after known options) are captured
    # via parse_known_args() in main() and stored in args.script_args
    return parser


def cmd_run(args: Namespace) -> int:
    """Execute the run command."""
    if args.module_name and args.script:
        logger.error("Cannot specify both script path and -m/--module")
        return 1
    if not args.module_name and not args.script:
        logger.error("Must specify either script path (-s/--script) or module name (-m/--module)")
        return 1

    if args.module_name:
        target = args.module_name
        is_module = True
    else:
        target = args.script
        is_module = False

    script_args: list[str] = args.script_args or []

    logger.info("Python: %s", sys.version)
    logger.info("Running: %s", target)
    logger.info("Mode: %s", "module" if is_module else "script")
    if script_args:
        logger.info("Script arguments: %s", " ".join(script_args))

    options = get_monitoring_options(args, duration_label="until script exits")
    if options is None:
        return 1

    def factory(control_address: str) -> ProcessFactory:
        return ChildProcessRunner(
            target=target,
            is_module=is_module,
            passthrough_args=script_args,
            control_address=control_address,
        )

    return run_monitoring_loop(
        factory=factory,
        wait_policy_factory=lambda: StartupTimeoutPolicy(2),
        options=options,
        address=args.control_name,
    )
