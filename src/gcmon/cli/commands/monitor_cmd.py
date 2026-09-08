"""Monitor command implementation."""

import argparse
import logging
import os
from argparse import Namespace

from gcmon.cli.commands.monitoring_base import run_monitoring_loop
from gcmon.cli.commands.monitoring_options import add_monitoring_options, get_monitoring_options
from gcmon.cli.shared.parser_factory import ParserFactory
from gcmon.monitoring.target_process import ExternalProcess, ProcessFactory
from gcmon.monitoring.wait_policy import StartupTimeoutPolicy

logger = logging.getLogger("gcmon")


def add_parser(parser_factory: ParserFactory) -> argparse.ArgumentParser:
    """Add the 'monitor' subparser and return it."""
    parser = parser_factory(
        "monitor",
        help="Monitor a process's garbage collection",
        description="Monitor Python's garbage collector and export statistics.",
    )
    parser.add_argument(
        "pid",
        type=int,
        help="Process ID to monitor",
    )
    add_monitoring_options(parser)
    parser.set_defaults(func=cmd_monitor)
    return parser


def cmd_monitor(args: Namespace) -> int:
    """Execute the monitor command."""
    pid = args.pid

    if pid < -1:
        logger.error("PID must be positive or -1, got %s", pid)
        return 1

    if pid == -1:
        pid = os.getpid()

    options = get_monitoring_options(args, duration_label="until interrupted (Ctrl+C)")
    if options is None:
        return 1

    def factory(control_address: str) -> ProcessFactory:
        return ExternalProcess(pid)

    return run_monitoring_loop(
        factory=factory,
        wait_policy_factory=lambda: StartupTimeoutPolicy(2),
        options=options,
        address=args.control_name,
    )
