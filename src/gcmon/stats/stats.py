from collections import deque
from collections.abc import Sequence

try:
    from ddsketch import DDSketch

    HAS_DDSKETCH = True
except ImportError:
    HAS_DDSKETCH = False


def get_quantile_value(buffer: Sequence[float], q: int) -> float:
    if not buffer:
        return 0.0

    idx = (q / 100.0) * (len(buffer) - 1)
    lower = int(idx)
    upper = lower + 1
    if upper >= len(buffer):
        return buffer[-1]
    weight = idx - lower
    return buffer[lower] * (1 - weight) + buffer[upper] * weight


class Stats:
    MAX_BUFFER_LEN = 1024
    REL_ACCURACY = 0.001

    def __init__(self) -> None:
        self._sketch: DDSketch | None = None
        if HAS_DDSKETCH:
            self._sketch = DDSketch(relative_accuracy=self.REL_ACCURACY)
        self._data: deque[float] = deque(maxlen=self.MAX_BUFFER_LEN)
        self._sum: float = 0
        self._count: int = 0
        self._percentiles: dict[int, float] | None = None

    def update(self, value: float) -> None:
        if self._percentiles is not None:
            raise RuntimeError("Cannot update Stats after materialize() has been called")

        if self._sketch is not None:
            self._sketch.add(value)

        self._data.append(value)

        self._sum += value
        self._count += 1

    def materialize(self) -> None:
        """Settle the percentiles and give the buffer back.

        Final. The caller settles a ring once its process has exited, so no
        value can arrive afterwards, and the four percentiles left behind cover
        every value this instance saw.
        """
        if self._percentiles is not None or self._count == 0:
            return
        sorted_data = sorted(self._data)
        self._percentiles = {
            50: get_quantile_value(sorted_data, 50),
            90: get_quantile_value(sorted_data, 90),
            95: get_quantile_value(sorted_data, 95),
            99: get_quantile_value(sorted_data, 99),
        }
        self._data.clear()
        self._sketch = None

    def average(self) -> float:
        if self._count == 0:
            return 0.0
        return self._sum / self._count

    def percentile(self, p: int) -> float:
        if not 0 <= p <= 100:
            raise ValueError(f"percentile must be in [0, 100], got {p}")
        if self._percentiles is not None:
            return self._percentiles.get(p, 0.0)
        if self._sketch is not None and self._count >= self.MAX_BUFFER_LEN:
            q = self._sketch.get_quantile_value(p / 100.0)
            assert q is not None, "None is an empty sketch, and this one is full"
            return q
        return get_quantile_value(sorted(self._data), p)

    def count(self) -> int:
        return self._count

    def sum(self) -> float:
        return self._sum

    @property
    def buffer(self) -> Sequence[float]:
        return self._data

    @property
    def has_sketch(self) -> bool:
        return self._sketch is not None

    @property
    def percentiles(self) -> dict[int, float] | None:
        return self._percentiles
