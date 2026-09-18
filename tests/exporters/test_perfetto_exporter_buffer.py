"""Tests for what a `PerfettoExporter` puts in its buffer."""

from __future__ import annotations

from pathlib import Path

from gcmon.exporters import PerfettoExporter
from gcmon.exporters.trace_converter import convert_item_to_trace_format
from gcmon.model.data import LossMsg
from gcmon.model.names import RSS, gc_loss_slice_name
from gcmon.model.trace_event import (
    Counter,
    Slice,
)
from tests.exporters.perfetto_helpers import pause_item
from tests.helpers import (
    create_mock_loss_item,
    create_mock_stats_item,
    interpreter_track,
    loss_track,
    proc,
    process_track,
)

# The process whose events fill the buffer.
TARGET_PID: int = 100


def _loss() -> LossMsg:
    """The smallest loss record: one collection missed, over one nanosecond.

    These tests count what the buffer flushed, not what a loss says, so the
    figures are as small as the record allows."""
    return create_mock_loss_item(iid=0, gen=0, ts_start=1, ts_stop=2, lost_count=1, lost_pause_ns=1)


class TestTheBufferHoldsNothingButEvents:
    """The exporter sends the encoder what the monitor gave it and nothing
    else. Which rows a trace draws is the encoder's to work out from the
    tracks those events name; see `TestATrackIsDescribedOffTheEventsOnIt` in
    `test_perfetto_format.py`."""

    def _make_exporter(self, tmp_path: Path) -> PerfettoExporter:
        return PerfettoExporter(tmp_path / "test.pb", flush_threshold=1000)

    def test_an_rss_sample_buffers_one_event(self, tmp_path: Path) -> None:
        exporter = self._make_exporter(tmp_path)

        exporter.add_rss_sample(proc(TARGET_PID), 4096, 1_000_000)

        assert len(exporter._buffer) == 1

    def test_a_second_rss_sample_buffers_a_second_event(self, tmp_path: Path) -> None:
        exporter = self._make_exporter(tmp_path)
        exporter.add_rss_sample(proc(TARGET_PID), 4096, 1_000_000)

        exporter.add_rss_sample(proc(TARGET_PID), 8192, 2_000_000)

        assert len(exporter._buffer) == 2

    def test_a_gc_record_buffers_the_events_the_converter_made(self, tmp_path: Path) -> None:
        exporter = self._make_exporter(tmp_path)
        item = pause_item()

        exporter.add_event(proc(TARGET_PID), item)

        assert exporter._buffer == convert_item_to_trace_format(proc(TARGET_PID), item)
        assert {e.track for e in exporter._buffer} == {interpreter_track(TARGET_PID, 0)}


class TestAddRssSample:
    def test_emits_counter_event_with_correct_shape(self, tmp_path: Path) -> None:
        exporter = PerfettoExporter(tmp_path / "test.pb", flush_threshold=1000)

        exporter.add_rss_sample(proc(TARGET_PID), 4096, 1_000_000)

        counters = [e for e in exporter._buffer if isinstance(e, Counter)]
        assert len(counters) == 1
        c = counters[0]
        assert c.track == process_track(TARGET_PID)
        assert c.metric == RSS
        assert c.display_name == RSS
        assert c.value == 4096
        assert c.ts == 1_000_000

    def test_two_pids_sample_onto_two_process_rows(self, tmp_path: Path) -> None:
        exporter = PerfettoExporter(tmp_path / "test.pb", flush_threshold=1000)

        exporter.add_rss_sample(proc(TARGET_PID), 4096, 1_000_000)
        exporter.add_rss_sample(proc(200), 8192, 2_000_000)

        assert {e.track for e in exporter._buffer} == {process_track(TARGET_PID), process_track(200)}


class TestAddLossEvent:
    def _make_exporter(self, tmp_path: Path) -> PerfettoExporter:
        return PerfettoExporter(tmp_path / "test.pb", flush_threshold=1000)

    def test_the_bar_is_the_window(self, tmp_path: Path) -> None:
        """The whole interval gcmon could not observe, not the 200 ns of GC
        known to be somewhere inside it."""
        exporter = self._make_exporter(tmp_path)

        exporter.add_loss_event(
            proc(TARGET_PID),
            create_mock_loss_item(iid=0, gen=0, ts_start=1_000, ts_stop=2_000, lost_count=1, lost_pause_ns=200),
        )

        span = next(e for e in exporter._buffer if isinstance(e, Slice))
        assert (span.name, span.ts_start, span.ts_stop) == (gc_loss_slice_name([0]), 1_000, 2_000)

    def test_it_lands_on_the_loss_track(self, tmp_path: Path) -> None:
        exporter = self._make_exporter(tmp_path)

        exporter.add_loss_event(
            proc(TARGET_PID),
            create_mock_loss_item(iid=1, gen=0, ts_start=1_000, ts_stop=2_000, lost_count=1, lost_pause_ns=200),
        )

        assert {e.track for e in exporter._buffer if isinstance(e, Slice)} == {loss_track(TARGET_PID, 1)}

    def test_it_does_not_share_the_track_with_gc_slices(self, tmp_path: Path) -> None:
        """One interpreter, two rows: a reconstructed span is easier to find
        on a row that holds nothing else."""
        exporter = self._make_exporter(tmp_path)

        exporter.add_event(proc(TARGET_PID), create_mock_stats_item(iid=0))
        exporter.add_loss_event(proc(TARGET_PID), _loss())

        assert {e.track for e in exporter._buffer if isinstance(e, Slice)} == {
            interpreter_track(TARGET_PID, 0),
            loss_track(TARGET_PID, 0),
        }

    def test_a_loss_event_names_no_interpreter_row(self, tmp_path: Path) -> None:
        """A `LossTrack` is not an `InterpreterTrack`, so a poll gcmon went blind in
        draws nothing on the interpreter's own row."""
        exporter = self._make_exporter(tmp_path)

        exporter.add_loss_event(proc(TARGET_PID), _loss())

        assert {e.track for e in exporter._buffer} == {loss_track(TARGET_PID, 0)}

    def test_two_interpreters_get_two_loss_tracks(self, tmp_path: Path) -> None:
        exporter = self._make_exporter(tmp_path)

        exporter.add_loss_event(proc(TARGET_PID), _loss())
        exporter.add_loss_event(
            proc(TARGET_PID), create_mock_loss_item(iid=1, gen=0, ts_start=1, ts_stop=2, lost_count=1, lost_pause_ns=1)
        )

        assert {e.track for e in exporter._buffer if isinstance(e, Slice)} == {
            loss_track(TARGET_PID, 0),
            loss_track(TARGET_PID, 1),
        }

    def test_the_loss_row_and_the_rss_row_are_not_the_same_row(self, tmp_path: Path) -> None:
        """Both belong to one pid and neither is an interpreter's own row.
        They are two track kinds rather than two reserved numbers, so this
        cannot be made to collide."""
        exporter = self._make_exporter(tmp_path)

        exporter.add_rss_sample(proc(TARGET_PID), 4096, 1_000)
        exporter.add_loss_event(proc(TARGET_PID), _loss())

        rss = next(e for e in exporter._buffer if isinstance(e, Counter))
        loss = next(e for e in exporter._buffer if isinstance(e, Slice))
        assert rss.track == process_track(TARGET_PID)
        assert loss.track == loss_track(TARGET_PID, 0)
