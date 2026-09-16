from gcmon.control.protocol import START_EVENT
from gcmon.model.data import InstantMsg


def create_instant_msg(name: str = START_EVENT, ts: int = 5_000_000) -> InstantMsg:
    return InstantMsg(type="i", name=name, ts=ts)
