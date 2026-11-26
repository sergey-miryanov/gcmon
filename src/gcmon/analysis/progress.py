import sys
from collections.abc import Iterable, Iterator
from time import monotonic


def report_progress(message: str) -> None:
    if sys.stderr.isatty():
        print(message, file=sys.stderr, flush=True)


def iter_with_progress[T](stage: str, iterable: Iterable[T], offset: int = 0) -> Iterable[T]:
    stream = sys.stderr
    if not stream.isatty():
        return iterable

    def updates() -> Iterator[T]:
        last = monotonic()
        count = offset
        for count, item in enumerate(iterable, offset + 1):
            yield item
            now = monotonic()
            if now - last >= 1:
                print(f"{stage} ({count:,} done)", file=stream, flush=True)
                last = now
        print(f"{stage} ({count:,} done)", file=stream, flush=True)

    return updates()
