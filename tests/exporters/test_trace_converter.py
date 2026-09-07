import pytest

from gcmon.exporters.trace_converter import convert_item_to_trace_format, duration_text, seen_text
from gcmon.model.trace_event import Counter, Slice
from tests.helpers import create_mock_new_incremental_item, create_mock_stats_item, proc


class TestDurationText:
    """The readable half of a pause total.

    Exactness lives in the `_ns` arg beside it; this one only has to be read
    correctly at a glance, which the digits are not.
    """

    @pytest.mark.parametrize(
        ("ns", "text"),
        [
            (3_316_458_100, "3s 316ms 458µs 100ns"),
            (5_000_000, "5ms"),
            (200, "200ns"),
            (1_000_000_100, "1s 100ns"),
            (90_000_000_000, "1m 30s"),
            (3_600_000_000_000, "1h"),
            (0, "0ns"),
        ],
    )
    def test_it_reads_as_a_duration(self, ns: int, text: str) -> None:
        assert duration_text(ns) == text

    def test_the_units_multiply_back_to_the_nanoseconds(self) -> None:
        """Every unit a component carries, against the number it came from.
        A wrong divisor produces text that still looks like a duration."""
        sizes = {"h": 3_600_000_000_000, "m": 60_000_000_000, "s": 1_000_000_000, "ms": 1_000_000, "µs": 1_000}

        for ns in (1, 999, 1_000, 3_316_458_100, 86_400_000_000_123):
            total = 0
            for part in duration_text(ns).split():
                digits = part.rstrip("hmsnµ")
                total += int(digits) * sizes.get(part.removeprefix(digits), 1)
            assert total == ns


class TestSeenText:
    """How much of an interval gcmon read, for a reader deciding whether to
    trust the bar's neighbours."""

    @pytest.mark.parametrize(
        ("observed", "lost", "text"),
        [
            (47, 7, "87.0% (47 of 54)"),
            (0, 5, "0.0% (0 of 5)"),
            (9, 0, "100.0% (9 of 9)"),
            (1, 2, "33.3% (1 of 3)"),
        ],
    )
    def test_it_reads_as_a_share_of_a_total(self, observed: int, lost: int, text: str) -> None:
        assert seen_text(observed, lost) == text

    def test_an_empty_interval_divides_by_nothing(self) -> None:
        """No collection ran and none was lost. A loss record never carries
        this, but the helper must not raise on the way to finding that out."""
        assert seen_text(0, 0) == "100.0% (0 of 0)"

    def test_the_total_is_what_ran_not_what_was_read(self) -> None:
        """The denominator is the reason this is worth writing out: a bare
        percentage says how bad the blindness was, not how much there was to
        be blind about."""
        assert seen_text(2, 98).endswith("(2 of 100)")


class TestNewIncrementalCounters:
    """The gauges the new incremental collector reports.

    Each is a property of one interpreter's collector state, not of a
    generation. ADR-0004 has the reasoning, from ``heap_size``.
    """

    _GAUGES = ("old_work", "survivor_count", "aging_threshold", "aging_spaces", "aging_next", "heap_size_stop")

    def _counters(self, **extra: int) -> dict[str, Counter]:
        events = convert_item_to_trace_format(proc(100), create_mock_new_incremental_item(**extra))
        return {e.metric: e for e in events if isinstance(e, Counter)}

    def _pause_args(self, **extra: int) -> dict[str, object]:
        events = convert_item_to_trace_format(proc(100), create_mock_new_incremental_item(**extra))
        pause = next(e for e in events if isinstance(e, Slice) and e.name.startswith("GC Pause"))
        return dict(pause.args)

    def test_every_gauge_gets_a_counter(self) -> None:
        counters = self._counters()
        for metric in self._GAUGES:
            assert metric in counters, f"{metric} has no counter"

    def test_the_display_name_repeats_the_metric(self) -> None:
        """The display name is the track identity; the metric keys the rank
        and the shared y axis (ADR-0005). A track whose halves disagree is
        queried under one name and read under another."""
        counters = self._counters()
        for metric in self._GAUGES:
            assert counters[metric].display_name == f"Thread 0 {metric}"

    def test_one_series_per_interpreter_not_per_generation(self) -> None:
        for gen in (0, 1, 2):
            counters = self._counters(gen=gen)
            for metric in self._GAUGES:
                assert counters[metric].display_name == f"Thread 0 {metric}"

    def test_two_interpreters_get_separate_tracks(self) -> None:
        assert self._counters(iid=1)["old_work"].display_name == "Thread 1 old_work"

    def test_increment_size_is_counted_only_for_the_young_generation(self) -> None:
        assert "new_increment_size" in self._counters(gen=1)
        assert "new_increment_size" not in self._counters(gen=2)

    def test_the_increment_size_counter_carries_the_records_value(self) -> None:
        counter = self._counters(gen=1, increment_size=4096)["new_increment_size"]
        assert counter.value == 4096
        assert counter.display_name == "Thread 0 new_increment_size"

    def test_the_gauges_reach_the_pause_args(self) -> None:
        """``heap_size`` is on the slice args for per-pause SQL (ADR-0004);
        these answer the same kind of question."""
        args = self._pause_args()
        for metric in (*self._GAUGES, "auto_collect"):
            assert metric in args

    def test_a_standard_build_record_carries_none_of_them(self) -> None:
        events = convert_item_to_trace_format(proc(100), create_mock_stats_item())
        metrics = {e.metric for e in events if isinstance(e, Counter)}
        assert metrics.isdisjoint({*self._GAUGES, "new_increment_size"})
