"""Item history / trend storage with the window lookups trigger functions need."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any

from trigger_tester.expression.period import Period
from trigger_tester.model import Item

MAX_NS = 999_999_999
Key = tuple[int, int]  # (clock, ns)


@dataclass(frozen=True)
class Point:
    clock: int
    ns: int
    value: Any
    meta: dict | None = None  # log items: timestamp, source, severity, logeventid

    @property
    def key(self) -> Key:
        return (self.clock, self.ns)


@dataclass(frozen=True)
class TrendPoint:
    clock: int  # start of the hour
    num: int
    value_min: float
    value_avg: float
    value_max: float


def convert_history_value(item: Item, raw: str) -> Any:
    if item.value_type == 0:
        return float(raw)
    if item.value_type == 3:
        return int(raw)
    return raw


def point_from_api(item: Item, record: dict) -> Point:
    meta = None
    if item.value_type == 2:
        meta = {
            "timestamp": int(record.get("timestamp", 0) or 0),
            "source": record.get("source", ""),
            "severity": int(record.get("severity", 0) or 0),
            "logeventid": int(record.get("logeventid", 0) or 0),
        }
    return Point(int(record["clock"]), int(record.get("ns", 0) or 0), convert_history_value(item, record["value"]), meta)


class Series:
    """Chronologically sorted history values of one item."""

    def __init__(self, item: Item, points: list[Point] | None = None) -> None:
        self.item = item
        unique = {p.key: p for p in points or []}
        self.points: list[Point] = [unique[k] for k in sorted(unique)]
        self.keys: list[Key] = [p.key for p in self.points]

    def __len__(self) -> int:
        return len(self.points)

    def index_upto(self, key: Key) -> int:
        """Number of points with key <= given key."""
        return bisect_right(self.keys, key)

    def last_n(self, key: Key, n: int) -> list[Point]:
        end = self.index_upto(key)
        return self.points[max(0, end - n) : end]

    def between(self, start_exclusive: Key, end_inclusive: Key) -> list[Point]:
        begin = bisect_right(self.keys, start_exclusive)
        end = bisect_right(self.keys, end_inclusive)
        return self.points[begin:end]

    def in_range(self, clock_from: int, clock_till: int) -> list[Point]:
        begin = bisect_left(self.keys, (clock_from, 0))
        end = bisect_right(self.keys, (clock_till, MAX_NS))
        return self.points[begin:end]

    def window(self, period: Period, clock: int, ns: int, tz: tzinfo | None) -> tuple[list[Point], Key]:
        """Values selected by ``period`` when evaluated at (clock, ns).

        Returns the points in chronological order and the effective end key.
        """
        end_clock = period.shift.apply(clock, tz) if period.shift is not None else clock
        end_key: Key = (end_clock, ns)
        if period.count is not None:
            return self.last_n(end_key, period.count), end_key
        if period.seconds is not None:
            # value cache: (end - seconds, end], a zero period selects nothing
            start_key: Key = (end_clock - period.seconds, ns)
            return self.between(start_key, end_key), end_key
        return self.last_n(end_key, 1), end_key


class TrendSeries:
    def __init__(self, item: Item, points: list[TrendPoint] | None = None) -> None:
        self.item = item
        unique = {p.clock: p for p in points or []}
        self.points = [unique[k] for k in sorted(unique)]
        self.clocks = [p.clock for p in self.points]

    def __len__(self) -> int:
        return len(self.points)

    def between(self, start_inclusive: int, end_exclusive: int) -> list[TrendPoint]:
        begin = bisect_left(self.clocks, start_inclusive)
        end = bisect_left(self.clocks, end_exclusive)
        return self.points[begin:end]


@dataclass
class ItemRequirements:
    """How much data must be preloaded for an item before the analysed period."""

    lookback_seconds: int = 0
    lookback_count: int = 2
    needs_history: bool = False
    needs_trends: bool = False
    trend_lookback_seconds: int = 0
    functions: list[str] = field(default_factory=list)

    def merge_seconds(self, seconds: int) -> None:
        self.lookback_seconds = max(self.lookback_seconds, seconds)

    def merge_count(self, count: int) -> None:
        self.lookback_count = max(self.lookback_count, count)
