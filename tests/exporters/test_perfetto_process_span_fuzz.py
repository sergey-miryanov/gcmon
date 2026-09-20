"""Differential fuzz for the widths on the ``Processes`` row.

Each process draws its span on a track of its own, so no span has to give
up an end to keep another (ADR-0011). Whether that survives depends on how
the trace processor pairs slices and folds the merged tracks, which no
wire-level test can check, so these ask it directly.

Marked ``fuzz``: each trial loads a trace, costing seconds rather than
milliseconds. Seeds are fixed, so a failure reproduces.
"""

import random
from pathlib import Path

import pytest

from gcmon.exporters.perfetto_builders import build_trace
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _emit_process_lifetime_slice,
    _emit_process_lifetime_track_descriptor,
    finalize_perfetto_packets,
    process_track_name,
)
from gcmon.exporters.perfetto_track_state import PerfettoTrackState, ProcessSpan
from tests.exporters.perfetto_helpers import span
from tests.helpers import misplaced_end_events, open_trace_processor

pytestmark = pytest.mark.fuzz

SEQUENCE_ID = 4242
TRIALS = 12


def _random_spans(rng: random.Random) -> list[ProcessSpan]:
    """Spans over a tiny coordinate space, so that crossings, equal starts,
    equal ends and zero-length spans are common rather than rare."""
    return [
        span(pid, start, start + rng.choice([0, 0, 1, 2, 5, 10]))
        for pid in range(100, 100 + rng.randint(2, 6))
        for start in (rng.randrange(0, 12),)
    ]


def _closeout(spans: list[ProcessSpan]) -> list[bytes]:
    """The packets the end of a trace holding *spans* writes."""
    state = PerfettoTrackState()
    for one in spans:
        state.update_process_lifetime(one.process, one.start_ts)
        state.update_process_lifetime(one.process, one.end_ts)
    return finalize_perfetto_packets(state, sequence_id=SEQUENCE_ID)


def _slices_as_read_back(packets: list[bytes], tmp_path: Path, name: str) -> tuple[int, dict[str, tuple[int, int]]]:
    """Load *packets* as a trace and return the ``Processes`` slices the
    trace processor reports, plus its ``misplaced_end_event`` counter."""
    path = tmp_path / f"{name}.pftrace"
    path.write_bytes(build_trace(packets))
    with open_trace_processor(path) as tp:
        misplaced = misplaced_end_events(tp)
        slices = {
            row.name: (row.ts, row.dur)
            for row in tp.query(
                f"SELECT s.name, s.ts, s.dur FROM slice s JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}'"
            )
        }
    return misplaced, slices


def _expected(spans: list[ProcessSpan]) -> dict[str, tuple[int, int]]:
    return {process_track_name(one.process): (one.start_ts, one.end_ts - one.start_ts) for one in spans}


@pytest.mark.parametrize("seed", range(TRIALS))
def test_every_span_reads_back_at_its_observed_width(seed: int, tmp_path: Path) -> None:
    """Whatever shape went in: each slice reads back at the pair gcmon
    observed, with no ``misplaced_end_event``.

    That stat has severity ``data_loss`` rather than ``error``, so a check
    filtering on ``error`` alone would never see a lost pairing.
    """
    spans = _random_spans(random.Random(seed))

    misplaced, slices = _slices_as_read_back(_closeout(spans), tmp_path, f"observed{seed}")

    assert misplaced == 0
    assert slices == _expected(spans)


@pytest.mark.parametrize("seed", range(TRIALS))
def test_no_slice_is_left_open(seed: int, tmp_path: Path) -> None:
    """``dur = -1`` is what an unpaired BEGIN reads as, and a zero-length
    span is the shape that risks it."""
    spans = _random_spans(random.Random(seed))

    _misplaced, slices = _slices_as_read_back(_closeout(spans), tmp_path, f"open{seed}")

    assert [name for name, (_ts, dur) in slices.items() if dur < 0] == []


def test_an_end_written_first_reads_as_a_negative_duration(tmp_path: Path) -> None:
    """The negative control for BEGIN-first, which is the one ordering rule
    left in the pass: without it the positive tests above would pass just as
    well in a world where the order of the pair did not matter.

    This is a claim about the trace processor, not about gcmon: one that
    paired a zero-length span written END-first would fail here while gcmon
    is fine.
    """
    state = PerfettoTrackState()
    instant = span(100, 5, 5)
    pair = _emit_process_lifetime_slice(instant, state, SEQUENCE_ID)
    packets = [_emit_process_lifetime_track_descriptor(instant.process, state, SEQUENCE_ID), *reversed(pair)]

    _misplaced, slices = _slices_as_read_back(packets, tmp_path, "end_first")

    assert slices == {process_track_name(instant.process): (5, -1)}, (
        "a zero-length span written END-first reads as an unclosed slice. Re-read "
        "ADR-0011's BEGIN-first rule before relaxing it."
    )
