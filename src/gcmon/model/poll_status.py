from enum import IntEnum, auto, unique


@unique
class PollStatus(IntEnum):
    OK = auto()
    FAIL = auto()
    INVALID_PROCESS = auto()
    CONTENDED = auto()
