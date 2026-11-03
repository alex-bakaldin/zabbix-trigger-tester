"""Parsing of Zabbix time periods, time shifts and durations.

Examples: ``5m``, ``#5``, ``5m:now-1h``, ``1d:now/d``, ``#3:now-1d``.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# Upper bounds used only to estimate how much history to prefetch.
UNIT_MAX_SECONDS = {**UNIT_SECONDS, "M": 31 * 86400, "y": 366 * 86400}


class PeriodError(ValueError):
    pass


_DURATION_RE = re.compile(r"^(\d+)([smhdw]?)$")
_PERIOD_RE = re.compile(r"^(\d+)([smhdwMy]?)$")


def parse_duration(text: str) -> int:
    """``300`` / ``5m`` / ``1d`` -> seconds."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise PeriodError(f"invalid time period {text!r}")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2) or "s"]


_SHIFT_RE = re.compile(r"^now((?:/[smhdwMy])|(?:[+-]\d+[smhdwMy]))*$")
_SHIFT_OP_RE = re.compile(r"/([smhdwMy])|([+-])(\d+)([smhdwMy])")


def _floor(dt: datetime, unit: str) -> datetime:
    if unit == "s":
        return dt.replace(microsecond=0)
    if unit == "m":
        return dt.replace(second=0, microsecond=0)
    if unit == "h":
        return dt.replace(minute=0, second=0, microsecond=0)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "d":
        return day
    if unit == "w":
        return day - timedelta(days=day.weekday())
    if unit == "M":
        return day.replace(day=1)
    if unit == "y":
        return day.replace(month=1, day=1)
    raise PeriodError(f"unknown time unit {unit!r}")


UNIT_ORDER = "smhdwMy"


def _add(dt: datetime, amount: int, unit: str) -> datetime:
    # zbx_tm_sub()/zbx_tm_add(): seconds .. weeks are added as epoch seconds,
    # months and years as calendar units with day-of-month clamping.
    if unit in UNIT_SECONDS:
        return datetime.fromtimestamp(dt.timestamp() + amount * UNIT_SECONDS[unit], dt.tzinfo)
    if unit == "M":
        month_index = dt.year * 12 + (dt.month - 1) + amount
        year, month = divmod(month_index, 12)
        month += 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    if unit == "y":
        year = dt.year + amount
        day = min(dt.day, calendar.monthrange(year, dt.month)[1])
        return dt.replace(year=year, day=day)
    raise PeriodError(f"unknown time unit {unit!r}")


@dataclass(frozen=True)
class TimeShift:
    """``now-1h``, ``now/d``, ``now/w-1w`` ..."""

    raw: str
    ops: tuple[tuple[str, int, str], ...]

    @classmethod
    def parse(cls, text: str) -> "TimeShift":
        text = text.strip()
        if not _SHIFT_RE.match(text):
            raise PeriodError(f"invalid time shift {text!r}")
        ops = []
        for match in _SHIFT_OP_RE.finditer(text[3:]):
            if match.group(1):
                ops.append(("/", 0, match.group(1)))
            else:
                ops.append((match.group(2), int(match.group(3)), match.group(4)))
        return cls(text, tuple(ops))

    def apply(self, clock: int, tz: tzinfo | None) -> int:
        dt = datetime.fromtimestamp(clock, tz)
        for op, amount, unit in self.ops:
            if op == "/":
                dt = _floor(dt, unit)
            else:
                dt = _add(dt, amount if op == "+" else -amount, unit)
        return int(dt.timestamp())

    def max_backward_seconds(self) -> int:
        total = 0
        for op, amount, unit in self.ops:
            if op == "/":
                total += UNIT_MAX_SECONDS[unit]
            elif op == "-":
                total += amount * UNIT_MAX_SECONDS[unit]
        return total

    def check_min_unit(self, unit: str) -> None:
        """Server rule: units used in the shift must not be smaller than the period unit."""
        for _, _, shift_unit in self.ops:
            if UNIT_ORDER.index(shift_unit) < UNIT_ORDER.index(unit):
                raise PeriodError("time units in time shift must be greater or equal to period time unit")

    def apply_datetime(self, clock: int, tz: tzinfo | None) -> datetime:
        dt = datetime.fromtimestamp(clock, tz)
        for op, amount, unit in self.ops:
            if op == "/":
                dt = _floor(dt, unit)
            else:
                dt = _add(dt, amount if op == "+" else -amount, unit)
        return dt


@dataclass(frozen=True)
class Period:
    """Second parameter of history functions: ``#N`` or ``<sec>`` with optional ``:shift``."""

    raw: str
    count: int | None = None
    seconds: int | None = None
    shift: TimeShift | None = None
    num: int | None = None  # period as <num><unit>, units s m h d w M y (M/y only for trends)
    unit: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Period":
        text = text.strip()
        base, _, shift_text = text.partition(":")
        shift = TimeShift.parse(shift_text) if shift_text else None
        base = base.strip()
        if not base:
            return cls(text, shift=shift)
        if base.startswith("#"):
            if not base[1:].isdigit() or int(base[1:]) < 1:
                raise PeriodError(f"invalid value count {base!r}")
            return cls(text, count=int(base[1:]), shift=shift)
        match = _PERIOD_RE.match(base)
        if not match:
            raise PeriodError(f"invalid time period {text!r}")
        num, unit = int(match.group(1)), match.group(2) or "s"
        seconds = num * UNIT_SECONDS[unit] if unit in UNIT_SECONDS else None
        return cls(text, seconds=seconds, shift=shift, num=num, unit=unit)

    @property
    def is_calendar(self) -> bool:
        return self.unit in ("M", "y")

    @property
    def is_empty(self) -> bool:
        return self.count is None and self.seconds is None

    def with_default(self, default: str | None) -> "Period":
        if not self.is_empty or default is None:
            return self
        base = Period.parse(default)
        return Period(self.raw or default, base.count, base.seconds, self.shift)

    def lookback_seconds(self) -> int:
        total = self.seconds or 0
        if self.is_calendar and self.num is not None:
            total = self.num * UNIT_MAX_SECONDS[self.unit]
        if self.shift:
            total += self.shift.max_backward_seconds()
        return total
