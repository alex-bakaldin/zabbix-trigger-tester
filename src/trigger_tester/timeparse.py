"""Parsing of CLI time arguments."""

from __future__ import annotations

import re
from datetime import datetime, tzinfo

from trigger_tester.expression.period import PeriodError, TimeShift, parse_duration

_RELATIVE_RE = re.compile(r"^(now)?([+-]\d+[smhdw])+$")


def parse_time(text: str, tz: tzinfo | None, now: int) -> int:
    """Accepts ``now``, ``now-1d``, ``-2h``, ``now/d``, unix timestamp, ISO date/time."""
    text = text.strip()
    if text == "now":
        return now
    if text.startswith("now"):
        try:
            return TimeShift.parse(text).apply(now, tz)
        except PeriodError:
            pass
    if _RELATIVE_RE.match(text):
        try:
            return TimeShift.parse("now" + text).apply(now, tz)
        except PeriodError:
            pass
    if text.isdigit() and len(text) >= 9:
        return int(text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"cannot parse time {text!r} (use ISO date/time, unix timestamp or now-1d)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz) if tz is not None else dt
    return int(dt.timestamp())


def parse_step(text: str) -> int | None:
    text = text.strip().lower()
    if text in ("0", "off", "none", "no"):
        return None
    if text == "auto":
        return -1
    return parse_duration(text)


__all__ = ["parse_time", "parse_step", "parse_duration"]
