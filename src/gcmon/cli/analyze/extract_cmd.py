"""Extract command implementation."""

import argparse
import logging
from argparse import Namespace
from pathlib import Path
from typing import Final

from gcmon.analysis.extract import COLUMNS, META_KEYS, META_SUFFIX, record_meta, record_rows, write_csv, write_meta
from gcmon.analysis.perfetto_prebuilt import MissingExtraError
from gcmon.analysis.pyperformance import benchmark_names, read_results, runs_of
from gcmon.analysis.trace_rows import TraceProcessorReader
from gcmon.cli.shared.parser_factory import ParserFactory

from ...support.vocabulary import CMD_EXTRACT, PROGRAM_NAME

logger = logging.getLogger(PROGRAM_NAME)

_CSV_SUFFIX: Final = ".csv"

_RESULTS_SUFFIX: Final = ".json"


def _meta_pair(text: str) -> tuple[str, str]:
    """`KEY=VALUE`, split at the first `=`."""
    key, separator, value = text.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key, value


def _column_list(text: str) -> list[str]:
    """`a,b,c` as its names. An empty one is refused rather than skipped."""
    names = text.split(",")
    if not all(names):
        raise argparse.ArgumentTypeError(f"empty column name in {text!r}")
    return names


def add_parser(parser_factory: ParserFactory) -> argparse.ArgumentParser:
    """Add the 'extract' subparser and return it."""
    parser = parser_factory(
        CMD_EXTRACT,
        help="Write a trace's records as a CSV table",
        description=(
            "Write one CSV row per record in a gcmon trace of pyperformance, with the "
            "benchmark each record's process ran and the counters as the record held "
            "them, and a .meta.json beside it saying where the table came from. Needs "
            "the analysis extra: pip install gcmon[analysis]"
        ),
    )
    parser.add_argument("trace", type=Path, help="A trace gcmon wrote")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output CSV path (default: the trace's path with .csv in place of its suffix)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="pyperformance's results file for the run (default: the trace's path with .json in place of its suffix)",
    )
    parser.add_argument(
        "--benchmark",
        action="append",
        dest="benchmarks",
        metavar="NAME",
        help="Keep only this benchmark's rows; repeat for more. A name the results file lacks stops the command",
    )
    parser.add_argument(
        "--meta",
        action="append",
        dest="metas",
        type=_meta_pair,
        metavar="KEY=VALUE",
        help="Add KEY to the .meta.json; repeat for more. A key gcmon derives stops the command",
    )
    columns = parser.add_mutually_exclusive_group()
    columns.add_argument(
        "--drop",
        action="extend",
        type=_column_list,
        metavar="COLUMN[,COLUMN...]",
        help="Leave these columns out of the table; repeat for more",
    )
    columns.add_argument(
        "--keep",
        action="extend",
        type=_column_list,
        metavar="COLUMN[,COLUMN...]",
        help="Write only these columns, with any other --keep; the table's own order holds",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output and its .meta.json")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable verbose output (use -v for INFO, -vv for DEBUG)",
    )
    parser.set_defaults(func=cmd_extract)
    return parser


def cmd_extract(args: Namespace) -> int:
    """Execute the extract command, returning its exit code."""
    trace: Path = args.trace
    output: Path = args.output or trace.with_suffix(_CSV_SUFFIX)
    sidecar = output.with_suffix(META_SUFFIX)
    results: Path = args.results or trace.with_suffix(_RESULTS_SUFFIX)
    extra: dict[str, str] = dict(args.metas or ())

    if not trace.is_file():
        logger.error("No trace at %s", trace)
        return 1
    if not results.is_file():
        logger.error("No results file at %s; name one with --results", results)
        return 1
    for path in (output, sidecar):
        if path.exists() and not args.overwrite:
            logger.error("%s exists; pass --overwrite to replace it", path)
            return 1
    derived = sorted(extra.keys() & set(META_KEYS))
    if derived:
        logger.error("gcmon derives %s; --meta cannot set it", ", ".join(derived))
        return 1
    chosen = set(args.drop or args.keep or ())
    unknown_columns = sorted(chosen - set(COLUMNS))
    if unknown_columns:
        logger.error("No such column: %s", ", ".join(unknown_columns))
        return 1
    if args.keep is not None:
        columns = [name for name in COLUMNS if name in chosen]
    else:
        columns = [name for name in COLUMNS if name not in chosen]
    if not columns:
        logger.error("--drop leaves no column to write")
        return 1

    try:
        document = read_results(results)
    except (OSError, ValueError) as error:
        logger.error("Cannot read the results file %s: %s", results, error)
        return 1
    runs = runs_of(document)
    only: set[str] | None = None
    if args.benchmarks is not None:
        only = set(args.benchmarks)
        unknown = sorted(only - {run.name for run in runs})
        if unknown:
            logger.error("Not in the results file %s: %s", results, ", ".join(unknown))
            return 1
    try:
        rows = TraceProcessorReader().read(trace)
    except MissingExtraError as error:
        logger.error("%s", error)
        return 1
    table = record_rows(rows, benchmark_names(rows.processes, runs), only)
    try:
        write_csv(output, table, overwrite=args.overwrite, columns=columns)
        write_meta(sidecar, {**record_meta(trace, document), **extra}, overwrite=args.overwrite)
    except FileExistsError as error:
        logger.error("%s exists; pass --overwrite to replace it", error.filename)
        return 1

    logger.info("Wrote %s records to %s and %s", len(table), output, sidecar)
    return 0
