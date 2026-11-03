"""Value helpers shared by the evaluator and trigger functions.

Zabbix expression values are numbers (double / uint64) or strings.  A failed
function or an operation on incompatible operands yields *Unknown*, which the
server turns into trigger state UNKNOWN while keeping the previous value.
"""

from __future__ import annotations

import math
from typing import Any

# zbx_double_compare(): fabs(a - b) <= ZBX_DOUBLE_EPSILON, an absolute epsilon.
DOUBLE_EPSILON = 2.22e-16

Number = int | float
Value = Any  # Number | str | Unknown


class Unknown:
    """Marker for a value that could not be computed (trigger becomes UNKNOWN)."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Unknown({self.reason!r})"

    def __bool__(self) -> bool:  # guard against accidental truth tests
        raise TypeError("Unknown has no truth value")


def is_unknown(value: Any) -> bool:
    return isinstance(value, Unknown)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def to_number(value: Any) -> Number | None:
    """Convert a value to a number, returning None when impossible."""
    if is_number(value):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    return None


def doubles_equal(a: float, b: float) -> bool:
    return abs(a - b) <= DOUBLE_EPSILON


def truthy(value: Any) -> bool | None:
    """Zabbix truth: numeric != 0.  None when the value is not numeric."""
    number = to_number(value)
    if number is None:
        return None
    return number != 0


def format_value(value: Any) -> str:
    if is_unknown(value):
        return "UNKNOWN"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value == 1.797693134862316e308:
            return "inf"
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.6g}" if abs(value) >= 1e6 or abs(value) < 1e-4 else f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)
