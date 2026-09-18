"""Tests for the JSONL file exporter."""

import json

from gcmon.analysis import jsonl_io
from gcmon.control.protocol import START_EVENT, STOP_EVENT
from gcmon.exporters import JsonlExporter
from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    CANDIDATES,
    COLLECTED,
    COLLECTIONS,
    DURATION,
    GEN,
    HEAP_SIZE,
    IID,
    PID,
    TS_START,
    TYPE,
    UNCOLLECTABLE,
)
from gcmon.support.vocabulary import ENCODING
from tests.conftest import DEFAULT_PID
from tests.data_helpers import create_instant_msg
from tests.exporters.conftest import ExporterFactory, JsonlFileReader
from tests.helpers import assert_is_instant_msg, create_mock_loss_item, create_mock_stats_item, proc


class TestJsonlExporter:
    def test_init_default_parameters(self, jsonl_exporter: ExporterFactory) -> None:
        exporter, path = jsonl_exporter()
        assert isinstance(exporter, JsonlExporter)
        assert exporter._flush_threshold == 100
        assert exporter._events == []
        assert exporter._output_path == path

    def test_init_custom_parameters(self, jsonl_exporter: ExporterFactory) -> None:
        exporter, path = jsonl_exporter(threshold=50)
        assert isinstance(exporter, JsonlExporter)
        assert exporter._flush_threshold == 50
        assert exporter._events == []
        assert exporter._output_path == path

    def test_add_event_json_output_format(self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader) -> None:
        exporter, path = jsonl_exporter(threshold=1)
        stats_item = create_mock_stats_item(
            gen=0,
            ts_start=1_000_000,
            ts_stop=1_005_000_000,
            collections=10,
            collected=5,
            uncollectable=0,
            candidates=15,
            heap_size=1024,
            duration=0.001,
        )
        exporter.add_event(proc(DEFAULT_PID), stats_item)
        exporter.close()

        events = read_jsonl(path)
        assert len(events) == 1
        event = events[0]
        assert event[PID] == 12345
        assert event[IID] == 0
        assert event[GEN] == 0
        assert event[TS_START] == 1000000
        assert event[COLLECTIONS] == 10
        assert event[COLLECTED] == 5
        assert event[UNCOLLECTABLE] == 0
        assert event[CANDIDATES] == 15
        assert event[HEAP_SIZE] == 1024
        assert event[DURATION] == 0.001

    def test_add_event_multiple_events(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1000)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()

        events = read_jsonl(path)
        assert len(events) == 3
        for event in events:
            assert event[PID] == 12345

    def test_close_flushes_events(self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory) -> None:
        exporter, path = jsonl_exporter(threshold=1000)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        assert path.exists()
        assert path.read_text() != ""

    def test_close_flushes_remaining_events(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=100)
        assert isinstance(exporter, JsonlExporter)
        for _ in range(3):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        assert len(exporter._events) == 3
        assert not path.exists()
        exporter.close()
        assert len(read_jsonl(path)) == 3

    def test_add_event_output_to_file(self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory) -> None:
        exporter, path = jsonl_exporter(threshold=1)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        assert path.exists()
        assert path.read_text() != ""

    def test_add_event_json_is_single_line(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1000)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        events = read_jsonl(path)
        assert len(events) == 2
        for event in events:
            assert PID in event

    def test_interpreter_id_in_output(self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader) -> None:
        exporter, path = jsonl_exporter(threshold=1)
        stats_item = create_mock_stats_item(iid=5678)
        exporter.add_event(proc(DEFAULT_PID), stats_item)
        exporter.close()
        event = read_jsonl(path)[0]
        assert event[IID] == 5678

    def test_pid_in_output(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1)
        exporter.add_event(proc(99999), mock_stats_item)
        exporter.close()
        event = read_jsonl(path)[0]
        assert event[PID] == 99999

    def test_close_multiple_calls_safe(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        exporter.close()
        assert len(read_jsonl(path)) == 1

    def test_add_event_after_close(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1000)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        assert len(read_jsonl(path)) == 2

    def test_close_with_no_events_does_not_create_file(self, jsonl_exporter: ExporterFactory) -> None:
        exporter, path = jsonl_exporter()
        exporter.close()
        assert not path.exists()


class TestJsonlExporterFlushThreshold:
    def test_events_buffered_until_threshold(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=10)
        for _ in range(5):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        assert not path.exists()
        for _ in range(5):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        assert len(read_jsonl(path)) == 10

    def test_flush_on_threshold_reached(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=5)
        for _ in range(4):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
            assert not path.exists()
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        assert len(read_jsonl(path)) == 5

    def test_each_full_batch_is_written(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=3)

        for _ in range(7):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        assert len(read_jsonl(path)) == 6

    def test_close_writes_the_last_partial_batch(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=3)
        for _ in range(7):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        exporter.close()

        assert len(read_jsonl(path)) == 7

    def test_threshold_one_writes_each_event(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1)

        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        assert len(read_jsonl(path)) == 1

    def test_threshold_one_appends_the_next_event(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        assert len(read_jsonl(path)) == 2

    def test_flush_on_threshold_reached_for_loss_events(
        self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=5)
        for _ in range(4):
            exporter.add_loss_event(proc(DEFAULT_PID), create_mock_loss_item())
            assert not path.exists()
        exporter.add_loss_event(proc(DEFAULT_PID), create_mock_loss_item())
        assert len(read_jsonl(path)) == 5

    def test_flush_on_threshold_reached_for_instant_events(
        self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=5)
        for _ in range(4):
            exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg())
            assert not path.exists()
        exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg())
        assert len(read_jsonl(path)) == 5


class TestJsonlExporterInstantEvents:
    def test_add_instant_event_json_output_format(
        self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=1)
        instant = create_instant_msg(name=START_EVENT, ts=1_500_000_000)
        exporter.add_instant_event(proc(DEFAULT_PID), instant)
        exporter.close()

        events = read_jsonl(path)
        assert len(events) == 1
        event = events[0]
        assert_is_instant_msg(
            event,
            pid=DEFAULT_PID,
            name=instant.name,
            ts=instant.ts,
        )

    def test_add_instant_event_multiple(self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader) -> None:
        exporter, path = jsonl_exporter(threshold=1000)
        for name in (START_EVENT, STOP_EVENT):
            exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg(name=name, ts=1000))
        exporter.close()

        events = read_jsonl(path)
        assert len(events) == 2
        for event, name in zip(events, (START_EVENT, STOP_EVENT), strict=True):
            assert_is_instant_msg(event, pid=DEFAULT_PID, name=name, ts=1_000)

    def test_mixed_instant_and_gc_events(
        self, mock_stats_item: GCStatsInfo, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        instant = create_instant_msg(name=STOP_EVENT, ts=2_000)
        exporter, path = jsonl_exporter(threshold=1_000)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.add_instant_event(proc(DEFAULT_PID), instant)
        exporter.close()

        events = read_jsonl(path)
        assert len(events) == 2
        assert events[0].get(TYPE) is None  # GC event has no type field
        assert events[1][TYPE] == "i"
        assert_is_instant_msg(
            events[1],
            pid=DEFAULT_PID,
            name=instant.name,
            ts=instant.ts,
        )

    def test_add_instant_event_flushes_at_threshold(
        self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=3)

        for _ in range(5):
            exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg(name="e", ts=1000))

        assert len(read_jsonl(path)) == 3

    def test_close_writes_the_instant_events_still_buffered(
        self, jsonl_exporter: ExporterFactory, read_jsonl: JsonlFileReader
    ) -> None:
        exporter, path = jsonl_exporter(threshold=3)
        for _ in range(5):
            exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg(name="e", ts=1000))

        exporter.close()

        assert len(read_jsonl(path)) == 5


class TestJsonlLossRecords:
    def test_a_loss_span_round_trips(self, jsonl_exporter: ExporterFactory) -> None:
        """The path `combine` depends on: a loss span written to JSONL has to
        come back as the same record, so a converted capture carries the spans
        the live run drew."""
        exporter, path = jsonl_exporter()
        msg = create_mock_loss_item(iid=1, gen=1, ts_start=1_000, ts_stop=2_000, lost_count=5, lost_pause_ns=8_100_000)

        exporter.add_loss_event(proc(DEFAULT_PID), msg)
        exporter.close()

        assert jsonl_io.read_jsonl(path) == {DEFAULT_PID: [msg]}

    def test_it_names_the_interpreter_that_lost_the_records(self, jsonl_exporter: ExporterFactory) -> None:
        """`iid` is the only thing on the line that says which interpreter
        went blind, and the loss row is drawn per interpreter."""
        exporter, path = jsonl_exporter()

        exporter.add_loss_event(
            proc(DEFAULT_PID), create_mock_loss_item(iid=1, gen=0, ts_start=1_000, ts_stop=2_000, lost_count=76)
        )
        exporter.close()

        record = json.loads(path.read_text(encoding=ENCODING))
        assert record[IID] == 1
        assert "tid" not in record

    def test_it_does_not_disturb_gc_records(self, jsonl_exporter: ExporterFactory) -> None:
        exporter, path = jsonl_exporter()
        item = create_mock_stats_item(iid=0)

        exporter.add_event(proc(DEFAULT_PID), item)
        exporter.add_loss_event(
            proc(DEFAULT_PID), create_mock_loss_item(iid=0, gen=0, ts_start=1, ts_stop=2, lost_count=1)
        )
        exporter.close()

        assert jsonl_io.read_jsonl(path)[DEFAULT_PID][0] == item
