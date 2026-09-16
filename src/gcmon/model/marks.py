"""The grammar of a mark: its formatter and its parser."""

import re
from enum import StrEnum
from typing import Final, NamedTuple

from ..support.vocabulary import PROGRAM_NAME

PREFIX: Final = PROGRAM_NAME


class Side(StrEnum):
    BEGIN = "begin"
    END = "end"


class Mark(NamedTuple):
    bench: str
    region: int
    phase_region: int
    side: Side


_NAME_CHARS: Final = "a-zA-Z0-9_-"
"""What a benchmark name may hold."""

_NOT_A_NAME: Final = re.compile(f"[^{_NAME_CHARS}]")
_MARK: Final = re.compile(f"{PREFIX}:([{_NAME_CHARS}]+):([0-9]+):([0-9]+):({'|'.join(Side)})")


def format_mark(bench: str, region: int, phase_region: int, side: Side) -> str:
    """The name of one mark."""
    return f"{PREFIX}:{_NOT_A_NAME.sub('_', bench) or '_'}:{region}:{phase_region}:{side}"


def parse_mark(name: str) -> Mark | None:
    """What *name* says, or ``None`` where it is not a mark."""
    found = _MARK.fullmatch(name)
    if found is None:
        return None

    bench, region, phase_region, side = found.groups()
    return Mark(bench, int(region), int(phase_region), Side(side))
