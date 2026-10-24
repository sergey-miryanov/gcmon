"""The run rows' bookkeeping, below what the trace processor can see."""

from collections.abc import Callable

import pytest

from gcmon.exporters.perfetto_format import encode_batch_args
from gcmon.exporters.perfetto_run_rows import _ALL_PAUSES_GROUP_NAME, _RSS_GROUP_NAME, draw_batch_on_run_rows
from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.model.names import GC_LOSS_CATEGORY, PAUSE, RSS
from gcmon.model.trace_event import Counter, Instant, Slice, TraceEvent
from tests.conftest import DEFAULT_PID
from tests.helpers import loss_track, pause_track, proc, process_track

_TS = 1_000_000

_PAUSE = Slice(pause_track(DEFAULT_PID, 0, 0), "GC Pause(0)", PAUSE.category, _TS, _TS + 1_000, {})

_LOSS = Slice(loss_track(DEFAULT_PID, 0), "GC Loss(0)", GC_LOSS_CATEGORY, _TS, _TS + 1_000, {})

_PROCESS = process_track(DEFAULT_PID)

_MARK = Instant(_PROCESS, "mark", _TS)

_RSS_SAMPLE = Counter(_PROCESS, RSS, RSS, _TS, 1_000_000)

_EARLY_ASKS: dict[str, tuple[TraceEvent, Callable[[PerfettoTrackState], int]]] = {
    "pause track": (_PAUSE, lambda state: state.get_or_create_run_track_uuid(_PAUSE.track)),
    "pause group": (_PAUSE, lambda state: state.get_or_create_run_group_uuid(_ALL_PAUSES_GROUP_NAME)),
    "loss track": (_LOSS, lambda state: state.get_or_create_run_track_uuid(_LOSS.track)),
    "mark track": (_MARK, lambda state: state.get_or_create_run_track_uuid(_PROCESS)),
    "rss counter": (_RSS_SAMPLE, lambda state: state.get_or_create_run_counter_track_uuid(_PROCESS)),
    "rss group": (_RSS_SAMPLE, lambda state: state.get_or_create_run_group_uuid(_RSS_GROUP_NAME)),
}


def _descriptors_drawn(event: TraceEvent, ask_first: Callable[[PerfettoTrackState], int] | None) -> list[bytes]:
    state = PerfettoTrackState()
    state.update_process_lifetime(proc(DEFAULT_PID), _TS)
    state.rank_processes([proc(DEFAULT_PID)])
    if ask_first is not None:
        ask_first(state)
    descriptors, _packets = draw_batch_on_run_rows([event], encode_batch_args([event]), state, sequence_id=1)
    return descriptors


@pytest.mark.parametrize(("event", "ask_first"), _EARLY_ASKS.values(), ids=_EARLY_ASKS.keys())
def test_asking_for_a_run_row_s_uuid_first_still_writes_its_descriptors(
    event: TraceEvent, ask_first: Callable[[PerfettoTrackState], int]
) -> None:
    drawn = _descriptors_drawn(event, None)

    assert drawn
    assert len(_descriptors_drawn(event, ask_first)) == len(drawn)
