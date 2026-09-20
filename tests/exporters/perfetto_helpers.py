"""Shared helpers for the Perfetto wire-format tests.

Each helper here is used by more than one of the ``test_perfetto_*``
modules. Anything used by a single module stays in that module.
"""

import functools
from collections.abc import Collection

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    TracePacket,
    TrackDescriptor,
    TrackEvent,
)

from gcmon.exporters.perfetto_format import convert_trace_events_to_perfetto
from gcmon.exporters.perfetto_process_lifetime import finalize_perfetto_packets
from gcmon.exporters.perfetto_track_state import PerfettoTrackState, ProcessSpan
from gcmon.exporters.trace_converter import convert_item_to_trace_format
from gcmon.model.data import GCStatsInfo
from gcmon.model.process import Process
from tests.helpers import create_mock_stats_item, proc

pause_item = functools.partial(
    create_mock_stats_item,
    ts_start=1_000,
    ts_stop=2_000,
    heap_size=1000,
    collections=1,
    collected=10,
    uncollectable=0,
    candidates=5,
    duration=0.001,
)
"""A pause record with figures small enough for an assertion to quote.

The builder in `tests/helpers.py` carries the realistic ones, which read as
noise in a test that asserts a timestamp landed on a packet. Only the
defaults differ; every field is still the same builder's.
"""


def span(pid: int, start_ts: int, end_ts: int, pid_epoch: int = 1) -> ProcessSpan:
    """A `ProcessSpan` for a test that names a pid rather than a process."""
    return ProcessSpan(proc(pid, pid_epoch), start_ts, end_ts)


def processes_row_uuids(state: PerfettoTrackState, *processes: Process) -> set[int]:
    """The uuids of the tracks *processes* draw their spans on, which the
    ``Processes`` row merges into one (ADR-0011)."""
    return {state.get_process_lifetime_track_uuid(process) for process in processes}


def convert_item(
    process: Process,
    item: GCStatsInfo,
    state: PerfettoTrackState,
    sequence_id: int = 1,
) -> tuple[list[bytes], list[bytes]]:
    """Convert one ``(process, item)`` as a single batch and finalize, so the
    returned packets include the whole ``Processes`` pair.

    Unlike ``convert_items``, this finalizes; a test that wants to see
    what convert emitted on its own wants that one instead.
    """
    descriptors, packets = convert_trace_events_to_perfetto(
        convert_item_to_trace_format(process, item),
        state,
        sequence_id,
    )
    packets.extend(finalize_perfetto_packets(state, sequence_id))
    return descriptors, packets


def convert_items(
    items: list[tuple[Process, GCStatsInfo]],
    state: PerfettoTrackState,
    sequence_id: int = 1,
) -> tuple[list[bytes], list[bytes], list[bytes]]:
    """Convert each ``(process, item)`` as its own batch, then finalize once,
    the way ``ProtobufEventEncoder`` does across flushes.

    Returns ``(descriptors, convert_packets, closeout_packets)`` so a
    test can tell what the convert passes emitted from what the single
    closeout emitted.
    """
    descriptors: list[bytes] = []
    packets: list[bytes] = []
    for process, item in items:
        batch_desc, batch_packets = convert_trace_events_to_perfetto(
            convert_item_to_trace_format(process, item),
            state,
            sequence_id,
        )
        descriptors.extend(batch_desc)
        packets.extend(batch_packets)
    return descriptors, packets, finalize_perfetto_packets(state, sequence_id)


def lifetime_slices(
    packets: list[bytes],
    track_uuids: Collection[int],
) -> list[tuple[int, int, str, dict[str, str | int]]]:
    """Return ``[(ts, type, name, annotations), ...]`` for the slice
    events on any of *track_uuids*, in packet order.

    A process's span and its ``Lifetime`` bar sit on tracks of their own, so
    reading the whole ``Processes`` row takes every process's uuid; see
    :func:`processes_row_uuids`."""
    out: list[tuple[int, int, str, dict[str, str | int]]] = []
    for p in packets:
        packet = TracePacket()
        packet.ParseFromString(p)
        if not packet.HasField("track_event"):
            continue
        track_event = packet.track_event
        if track_event.track_uuid not in track_uuids:
            continue
        if track_event.type not in (
            TrackEvent.Type.TYPE_SLICE_BEGIN,
            TrackEvent.Type.TYPE_SLICE_END,
        ):
            continue
        annotations: dict[str, str | int] = {}
        for ann in track_event.debug_annotations:
            # A bool arrives as a bool, so a test can tell `clipped` false
            # from an int 0.
            annotations[ann.name] = getattr(ann, ann.WhichOneof("value") or "int_value")
        out.append((packet.timestamp, track_event.type, track_event.name or "", annotations))
    return out


def parse_track_descriptor(packet_bytes: bytes) -> TrackDescriptor | None:
    """Extract the inner ``TrackDescriptor`` from a ``TracePacket``.

    Returns ``None`` if the packet is not a track-descriptor packet.
    """
    packet = TracePacket()
    packet.ParseFromString(packet_bytes)
    return packet.track_descriptor if packet.HasField("track_descriptor") else None
