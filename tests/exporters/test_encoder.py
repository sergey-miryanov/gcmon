"""Tests for pluggable event encoders in ``gcmon.exporters.encoder``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from gcmon.exporters.encoder import (
    ProtobufEventEncoder,
    convert_trace_events_to_perfetto,  # noqa: F401  (used via monkeypatch.setattr)
)
from gcmon.model.trace_event import Instant
from tests.helpers import proc, process_track


class TestProtobufEventEncoder:
    def test_write_events_empty_no_file_created(self, tmp_path: Path) -> None:
        enc = ProtobufEventEncoder()
        path = tmp_path / "out.perfetto"
        enc.open(path)

        enc.write_events([])
        enc.close()

        assert not path.exists()

    def test_liveness_alone_still_produces_a_trace(self, tmp_path: Path) -> None:
        """``close()`` gates on having packets to emit, not on having
        written earlier: ``record_process_liveness`` reaches the span
        accumulator without passing through ``write_events``."""
        enc = ProtobufEventEncoder()
        path = tmp_path / "out.perfetto"
        enc.open(path)
        enc.record_process_liveness({proc(1234)}, 1_400_000_000)
        assert enc._has_written is False

        enc.close()

        assert path.exists() and path.stat().st_size > 0

    def test_closing_one_that_never_opened_writes_nowhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With liveness recorded there is a track to emit and no path to
        emit it to, which is a quiet return and not the writer's assertion."""
        monkeypatch.chdir(tmp_path)
        enc = ProtobufEventEncoder()
        enc.record_process_liveness({proc(1234)}, 1_400_000_000)

        enc.close()

        assert list(tmp_path.iterdir()) == []

    def test_reopening_is_refused(self, tmp_path: Path) -> None:
        """One encoder writes one trace. A reused one would drop the
        second trace's descriptors and its whole ``Processes`` track
        without raising -- hence a guard, not just a docstring."""
        enc = ProtobufEventEncoder()
        enc.open(tmp_path / "first.perfetto")

        with pytest.raises(AssertionError, match="one encoder writes one trace"):
            enc.open(tmp_path / "second.perfetto")

    def test_write_events_returns_early_when_converter_produces_no_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enc = ProtobufEventEncoder()
        path = tmp_path / "out.perfetto"
        enc.open(path)
        monkeypatch.setattr(
            "gcmon.exporters.encoder.convert_trace_events_to_perfetto",
            Mock(return_value=([], [])),
        )

        enc.write_events([Instant(process_track(1234), "ev", ts=1_000)])
        enc.close()

        assert not path.exists()
        assert enc._has_written is False
