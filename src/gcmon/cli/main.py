"""Command-line interface for gcmon."""

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Any

from ._version import installed_version
from .analyze import add_combine_parser
from .monitor import (
    add_monitor_parser,
    add_run_parser,
)


class _VersionAction(argparse.Action):
    """Print gcmon's version and exit.

    ``argparse``'s ``version`` action wants the string when the flag is declared, so every run
    would read the distribution's metadata. This one reads it when the flag is used.
    """

    def __init__(self, option_strings: Sequence[str], dest: str, help: str | None = None) -> None:
        super().__init__(option_strings=list(option_strings), dest=dest, nargs=0, help=help)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        sys.stdout.write(f"{installed_version()}\n")
        parser.exit()


def _create_parser() -> argparse.ArgumentParser:
    """Create the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="gcmon",
        description="Monitor Python's garbage collector and export statistics.",
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
        dest=argparse.SUPPRESS,
        help="Print the installed gcmon version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    add_monitor_parser(subparsers.add_parser)
    add_combine_parser(subparsers.add_parser)
    add_run_parser(subparsers.add_parser)

    return parser


def _setup_logging(verbose_count: int) -> None:
    """Configure logging for the CLI.

    Args:
        verbose_count: Verbose level count:
            0 = WARNING (default)
            1 = INFO (-v)
            2+ = DEBUG (-vv or more)
    """
    if verbose_count >= 2:
        level = logging.DEBUG
    elif verbose_count == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logger = logging.getLogger("gcmon")
    logger.setLevel(level)

    # Only add handler if none exists
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        formatter = logging.Formatter("[%(name)s] %(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    else:
        # Update existing handlers
        for handler in logger.handlers:  # type: ignore[assignment]
            handler.setLevel(level)


def _split_run_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split run command args at the first target option (-m/-s/--module/--script).

    Everything up to and including the target option + value goes to gcmon.
    Everything after is passed verbatim to the script.

    Args:
        argv: Command-line arguments starting with "run"

    Returns:
        Tuple of (gcmon args, script args)
    """
    target_options = {"-m", "--module", "-s", "--script"}
    for i, arg in enumerate(argv):
        if arg in target_options:
            # -m value or -s value → split after the value
            return argv[: i + 2], argv[i + 2 :]
        if arg.startswith("--module=") or arg.startswith("--script="):
            # --module=value or --script=value → split after this arg
            return argv[: i + 1], argv[i + 1 :]
    # No target option found: all args go to gcmon
    return argv, []


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    parser = _create_parser()

    # Check if "run" command is being used - need special handling for script args
    if argv is None:
        argv = sys.argv[1:]

    # For run command, split args at the first target option (-m/-s/--module/--script)
    # Everything before goes to gcmon, everything after goes to the script
    if argv and argv[0] == "run":
        gc_args, script_args = _split_run_args(argv)
        args = parser.parse_args(gc_args)
        args.script_args = script_args
    else:
        args = parser.parse_args(argv)

    # Setup logging before any logging calls
    _setup_logging(args.verbose)

    # Dispatch via args.func (set by each subparser's set_defaults)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
