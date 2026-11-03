"""Pure-Python implementations of Zabbix 7.0 trigger functions.

This is the fallback engine; the reference is the server code linked into
``native/libzbxtester.so``.  Every function here follows the corresponding
``evaluate_*()`` in ``src/libs/zbxexpression/evalfunc.c`` of Zabbix 7.0,
including error conditions, so that both engines give the same answers.

Values arriving from history are ordered newest-first inside the functions,
exactly like the vectors the server gets from its value cache.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Callable

from trigger_tester.expression import prediction
from trigger_tester.expression.ast import HistoryCall
from trigger_tester.expression.period import UNIT_SECONDS, Period, PeriodError, _add
from trigger_tester.history import Point, Series, TrendSeries
from trigger_tester.values import DOUBLE_EPSILON, doubles_equal, is_number, to_number

DBL_MAX = prediction.DBL_MAX
MATH_ERROR = prediction.MATH_ERROR
FLOAT, CHAR, LOG, UINT, TEXT = 0, 1, 2, 3, 4
NUMERIC_TYPES = (FLOAT, UINT)
UNIT_MULTIPLIERS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, **UNIT_SECONDS}


class FunctionError(Exception):
    pass


# ---------------------------------------------------------------------------
# parameter helpers (funcparam.c / zbxnum)
# ---------------------------------------------------------------------------

_TIME_SUFFIX_RE = re.compile(r"^(\d+)([smhdw]?)$")
_DOUBLE_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?([KMGTsmhdw]?)$")
_UINT_UNITS_RE = re.compile(r"^(\d+)([KMGTsmhdw]?)$")


def time_suffix(text: str) -> int:
    """zbx_is_time_suffix(): ``300``, ``5m`` ... (seconds, minutes, hours, days, weeks)."""
    match = _TIME_SUFFIX_RE.match(text.strip())
    if not match:
        raise FunctionError(f"invalid time period {text!r}")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2) or "s"]


def double_suffix(text: str, allow_suffix: bool = True) -> float:
    """zbx_is_double_suffix() + zbx_str2double()."""
    match = _DOUBLE_RE.match(text.strip())
    if not match or (match.group(4) and not allow_suffix):
        raise FunctionError(f"{text!r} is not a valid numeric value")
    number = float(text.strip()[: -1] if match.group(4) else text.strip())
    return number * UNIT_MULTIPLIERS[match.group(4)] if match.group(4) else number


def uint_with_units(text: str) -> int:
    match = _UINT_UNITS_RE.match(text.strip())
    if not match:
        raise FunctionError(f"{text!r} is not a valid unsigned value")
    return int(match.group(1)) * (UNIT_MULTIPLIERS[match.group(2)] if match.group(2) else 1)


def uint_plain(text: str) -> int:
    text = text.strip()
    if not text.isdigit():
        raise FunctionError(f"{text!r} is not a valid unsigned value")
    return int(text)


class Frame:
    """Everything a history function needs for one evaluation."""

    def __init__(
        self,
        call: HistoryCall,
        series: Series | None,
        trends: TrendSeries | None,
        clock: int,
        ns: int,
        tz: tzinfo | None,
    ) -> None:
        self.call = call
        self.series = series
        self.trends = trends
        self.clock = clock
        self.ns = ns
        self.tz = tz

    @property
    def period(self) -> Period:
        assert self.call.period is not None
        return self.call.period

    @property
    def value_type(self) -> int:
        return self.call.item.value_type if self.call.item is not None else FLOAT

    @property
    def name(self) -> str:
        return self.call.name

    def nparams(self) -> int:
        """Number of parameters after the item, like zbx_function_param_parse_count()."""
        if self.call.period_raw is None and not self.call.args:
            return 0
        return 1 + len(self.call.args)

    def arg(self, index: int, default: str | None = None) -> str | None:
        """Parameter #index (0 = first after the period) as unquoted text."""
        if index < len(self.call.args) and self.call.args[index] is not None:
            return self.call.args[index]
        return default

    def require_numeric(self) -> None:
        if self.value_type not in NUMERIC_TYPES:
            raise FunctionError("invalid value type")

    def end_ts(self) -> tuple[int, int]:
        """Evaluation end after applying the time shift (ts_end in evalfunc.c)."""
        if self.period.shift is not None:
            return self.period.shift.apply(self.clock, self.tz), self.ns
        return self.clock, self.ns

    def values(self) -> list[Point]:
        """Values selected by the period, newest first (value cache order)."""
        if self.series is None:
            raise FunctionError("cannot get values from value cache")
        points, _ = self.series.window(self.period, self.clock, self.ns, self.tz)
        return list(reversed(points))

    def last_n(self, n: int) -> list[Point]:
        if self.series is None:
            raise FunctionError("cannot get values from value cache")
        end_clock, end_ns = self.end_ts()
        return list(reversed(self.series.last_n((end_clock, end_ns), n)))

    def numbers(self, points: list[Point]) -> list[float]:
        return [float(p.value) if self.value_type == FLOAT else int(p.value) for p in points]


# ---------------------------------------------------------------------------
# count() pattern matching (zbxeval/count_pattern.c)
# ---------------------------------------------------------------------------

OP_ANY, OP_EQ, OP_NE, OP_GT, OP_GE, OP_LT, OP_LE, OP_LIKE, OP_REGEXP, OP_IREGEXP, OP_BITAND = (
    "any",
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "like",
    "regexp",
    "iregexp",
    "bitand",
)
_STRING_OPS = (OP_LIKE, OP_REGEXP, OP_IREGEXP, OP_EQ, OP_NE)


@dataclass
class CountPattern:
    op: str
    numeric: bool
    pattern: str
    number: float | int = 0
    mask: int = 0
    regex: re.Pattern | None = None


def init_count_pattern(operator: str | None, pattern: str | None, value_type: int) -> CountPattern:
    numeric = value_type in NUMERIC_TYPES
    if not operator:
        if not pattern:
            return CountPattern(OP_ANY, numeric, "")
        op = OP_EQ if numeric else OP_LIKE
    elif operator in (OP_EQ, OP_NE, OP_GT, OP_GE, OP_LT, OP_LE, OP_LIKE, OP_REGEXP, OP_IREGEXP, OP_BITAND):
        op = operator
    else:
        raise FunctionError(f'invalid operator "{operator}"')
    if not pattern and op in (OP_LIKE, OP_REGEXP, OP_IREGEXP):
        return CountPattern(OP_ANY, numeric, "")
    numeric = numeric and op not in (OP_REGEXP, OP_IREGEXP)
    cp = CountPattern(op, numeric, pattern or "")
    if numeric:
        if operator and not pattern:
            raise FunctionError(f'pattern must be provided for operator "{operator}"')
        if op == OP_LIKE:
            raise FunctionError(f'operator "{operator}" is not supported for counting numeric values')
        if op == OP_BITAND and value_type == FLOAT:
            raise FunctionError(f'operator "{operator}" is not supported for counting float values')
        text = pattern or ""
        mask_text = None
        if op == OP_BITAND and "/" in text:
            text, _, mask_text = text.partition("/")
        if text:
            if value_type == UINT:
                if op != OP_BITAND:
                    cp.number = uint_with_units(text)
                else:
                    cp.number = uint_plain(text)
                    cp.mask = uint_plain(mask_text) if mask_text else cp.number
            else:
                cp.number = double_suffix(text)
    else:
        if op not in _STRING_OPS:
            raise FunctionError(f'operator "{operator}" is not supported for counting string values')
        if op in (OP_REGEXP, OP_IREGEXP):
            if cp.pattern.startswith("@"):
                raise FunctionError(f'global regular expression "{cp.pattern[1:]}" is not available in trigger-tester')
            try:
                cp.regex = re.compile(cp.pattern, re.IGNORECASE if op == OP_IREGEXP else 0)
            except re.error as exc:
                raise FunctionError(f"invalid regular expression: {exc}") from None
    return cp


def _format_number(value: Any, value_type: int) -> str:
    if value_type == FLOAT:
        return f"{float(value):.4f}"
    return str(int(value))


def count_match(cp: CountPattern, value: Any, value_type: int) -> bool:
    if cp.op == OP_ANY:
        return True
    if cp.numeric:
        if value_type == UINT:
            v, p = int(value), int(cp.number)
            if cp.op == OP_EQ:
                return v == p
            if cp.op == OP_NE:
                return v != p
            if cp.op == OP_GT:
                return v > p
            if cp.op == OP_GE:
                return v >= p
            if cp.op == OP_LT:
                return v < p
            if cp.op == OP_LE:
                return v <= p
            return (v & cp.mask) == p
        v, p = float(value), float(cp.number)
        if cp.op == OP_EQ:
            return doubles_equal(v, p)
        if cp.op == OP_NE:
            return not doubles_equal(v, p)
        if cp.op == OP_GT:
            return v - p > DOUBLE_EPSILON
        if cp.op == OP_GE:
            return v - p >= -DOUBLE_EPSILON
        if cp.op == OP_LT:
            return p - v > DOUBLE_EPSILON
        if cp.op == OP_LE:
            return p - v >= -DOUBLE_EPSILON
        return False
    text = _format_number(value, value_type) if value_type in NUMERIC_TYPES else str(value)
    if cp.op == OP_EQ:
        return text == cp.pattern
    if cp.op == OP_NE:
        return text != cp.pattern
    if cp.op == OP_LIKE:
        return cp.pattern in text
    assert cp.regex is not None
    return cp.regex.search(text) is not None


# ---------------------------------------------------------------------------
# history functions
# ---------------------------------------------------------------------------


def fn_last(frame: Frame) -> Any:
    n = frame.period.count or 1  # a time period defaults to last(#1), like the server
    points = frame.last_n(n)
    if len(points) < n:
        raise FunctionError("not enough data")
    return points[n - 1].value


def fn_first(frame: Frame) -> Any:
    if frame.nparams() != 1:
        raise FunctionError("invalid number of parameters")
    if frame.period.count is not None:
        raise FunctionError("the first argument cannot be number of value")
    if frame.period.seconds is None:
        raise FunctionError("the first argument is not specified")
    if frame.period.seconds <= 0:
        raise FunctionError("the first argument must be greater than 0")
    points = frame.values()
    if not points:
        raise FunctionError("not enough data")
    return points[-1].value


def _check_range_params(frame: Frame, nparams: int = 1) -> None:
    if frame.nparams() != nparams:
        raise FunctionError("invalid number of parameters")
    if frame.period.is_empty:
        raise FunctionError("invalid second parameter")


def fn_min(frame: Frame) -> Any:
    frame.require_numeric()
    _check_range_params(frame)
    values = frame.numbers(frame.values())
    if not values:
        raise FunctionError("not enough data")
    return min(values)


def fn_max(frame: Frame) -> Any:
    frame.require_numeric()
    _check_range_params(frame)
    values = frame.numbers(frame.values())
    if not values:
        raise FunctionError("not enough data")
    return max(values)


def fn_avg(frame: Frame) -> Any:
    frame.require_numeric()
    _check_range_params(frame)
    values = frame.numbers(frame.values())
    if not values:
        raise FunctionError("not enough data")
    if frame.value_type == FLOAT:
        avg = 0.0
        for i, v in enumerate(values):  # running mean, newest first, as in evaluate_AVG()
            avg += v / (i + 1) - avg / (i + 1)
        return avg
    return sum(values) / len(values)


def fn_sum(frame: Frame) -> Any:
    frame.require_numeric()
    _check_range_params(frame)
    values = frame.numbers(frame.values())
    if frame.value_type == FLOAT:
        total = 0.0
        for v in values:
            total += v
        return total
    return sum(values)


def _count(frame: Frame, limit: int, unique: bool) -> Any:
    if frame.nparams() > 3:
        raise FunctionError("invalid number of parameters")
    cp = init_count_pattern(frame.arg(0), frame.arg(1), frame.value_type)
    period = frame.period
    if period.is_empty:
        period = Period(period.raw, count=1, shift=period.shift)
    if frame.series is None:
        raise FunctionError("cannot get values from value cache")
    points, _ = frame.series.window(period, frame.clock, frame.ns, frame.tz)
    values = [p.value for p in reversed(points)]
    if unique:
        seen: list[Any] = []
        for v in values:
            if frame.value_type == FLOAT:
                if any(doubles_equal(v, s) for s in seen):
                    continue
            elif v in seen:
                continue
            seen.append(v)
        values = seen
    count = 0
    for v in values:
        if count >= limit:
            break
        if count_match(cp, v, frame.value_type):
            count += 1
    return count


def fn_count(frame: Frame) -> Any:
    return _count(frame, 2**31 - 1, False)


def fn_countunique(frame: Frame) -> Any:
    return _count(frame, 2**31 - 1, True)


def fn_find(frame: Frame) -> Any:
    return _count(frame, 1, False)


def fn_nodata(frame: Frame) -> Any:
    if frame.nparams() > 2:
        raise FunctionError("invalid number of parameters")
    seconds = frame.period.seconds
    if frame.period.count is not None or seconds is None or seconds <= 0:
        raise FunctionError("invalid second parameter")
    mode = frame.arg(0, "")
    if mode not in ("", "strict"):
        raise FunctionError("invalid third parameter")
    if frame.series is None:
        return 1
    # "now" is the evaluation time; the virtual clock of the native engine does the same
    start, end = (frame.clock - seconds, frame.ns), (frame.clock, frame.ns)
    return 0 if frame.series.between(start, end) else 1


def fn_change(frame: Frame) -> Any:
    if frame.series is None:
        raise FunctionError("cannot get values from value cache")
    points = list(reversed(frame.series.last_n((frame.clock, frame.ns), 2)))
    if len(points) < 2:
        raise FunctionError("cannot get values from value cache")
    last, prev = points[0].value, points[1].value
    if frame.value_type == FLOAT:
        return float(last) - float(prev)
    if frame.value_type == UINT:
        return int(last) - int(prev)
    return 0 if str(last) == str(prev) else 1


def fn_changecount(frame: Frame) -> Any:
    if not 1 <= frame.nparams() <= 2:
        raise FunctionError("invalid number of parameters")
    if frame.period.is_empty:
        raise FunctionError("invalid second parameter")
    mode = frame.arg(0, "") or "all"
    if mode not in ("all", "inc", "dec"):
        raise FunctionError("invalid third parameter")
    points = frame.values()
    if len(points) < 2:
        raise FunctionError("not enough data")
    count = 0
    for i in range(len(points) - 1):
        last, prev = points[i].value, points[i + 1].value  # newest first
        if frame.value_type == UINT:
            changed = prev != last if mode == "all" else (prev < last if mode == "inc" else prev > last)
        elif frame.value_type == FLOAT:
            if mode == "all":
                changed = not doubles_equal(prev, last)
            else:
                changed = not doubles_equal(prev, last) and (prev < last if mode == "inc" else prev > last)
        elif frame.value_type in (CHAR, TEXT, LOG):
            changed = str(prev) != str(last)
        else:
            raise FunctionError("invalid value type")
        if changed:
            count += 1
    return count


def fn_fuzzytime(frame: Frame) -> Any:
    frame.require_numeric()
    if frame.nparams() > 1:
        raise FunctionError("invalid number of parameters")
    seconds = frame.period.seconds
    if frame.period.count is not None or seconds is None or seconds <= 0:
        raise FunctionError("invalid second parameter")
    if frame.clock <= seconds:
        raise FunctionError("invalid argument type or value")
    if frame.series is None:
        raise FunctionError("cannot get value from value cache")
    points = frame.series.last_n((frame.clock, frame.ns), 1)
    if not points:
        raise FunctionError("cannot get value from value cache")
    value = points[0].value
    return 1 if frame.clock - seconds <= value <= frame.clock + seconds else 0


def fn_bitand(frame: Frame) -> Any:
    if frame.value_type != UINT:
        raise FunctionError("invalid value type")
    if frame.nparams() > 2:
        raise FunctionError("invalid number of parameters")
    mask_text = frame.arg(0)
    if mask_text is None:
        raise FunctionError("invalid third parameter")
    mask = uint_plain(mask_text)
    return int(fn_last(frame)) & mask


def fn_percentile(frame: Frame) -> Any:
    frame.require_numeric()
    _check_range_params(frame, 2)
    percentage = double_suffix(frame.arg(0) or "", allow_suffix=False)
    if not 0.0 <= percentage <= 100.0:
        raise FunctionError("invalid third parameter")
    values = sorted(frame.numbers(frame.values()))
    if not values:
        raise FunctionError("not enough data")
    index = 1 if percentage == 0 else math.ceil(len(values) * (percentage / 100))
    return values[index - 1]


def _is_normal(value: float) -> bool:
    return not (math.isnan(value) or math.isinf(value))


def _statistical(name: str, min_values: int, calc: Callable[[list[float]], float]) -> Callable[[Frame], Any]:
    def impl(frame: Frame) -> Any:
        frame.require_numeric()
        _check_range_params(frame)
        values = frame.numbers(frame.values())
        if len(values) < min_values:
            raise FunctionError("not enough data")
        try:
            result = calc(values)
        except (statistics.StatisticsError, ZeroDivisionError, ValueError, OverflowError):
            raise FunctionError(f"cannot calculate {name}() value") from None
        if not _is_normal(result):
            raise FunctionError(f"cannot calculate {name}() value")
        return result

    return impl


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _kurtosis(values: list[float]) -> float:
    mean = _mean(values)
    m2 = sum((v - mean) ** 2 for v in values) / len(values)
    m4 = sum((v - mean) ** 4 for v in values) / len(values)
    if m2 * m2 == 0.0 or not _is_normal(m2 * m2):
        raise ValueError
    return m4 / (m2 * m2)


def _skewness(values: list[float]) -> float:
    mean = _mean(values)
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    divisor = len(values) * std**3
    if divisor == 0.0 or not _is_normal(divisor):
        raise ValueError
    return sum((v - mean) ** 3 for v in values) / divisor


def _median(values: list[float]) -> float:
    values = sorted(values)
    n = len(values)
    return (values[n // 2 - 1] + values[n // 2]) / 2.0 if n % 2 == 0 else values[n // 2]


def _mad(values: list[float]) -> float:
    median = _median(values)
    return _median([abs(v - median) for v in values])


def fn_rate(frame: Frame) -> Any:
    frame.require_numeric()
    _check_range_params(frame)
    points = frame.values()
    if len(points) < 2:
        raise FunctionError("not enough data")
    values = frame.numbers(points)
    first_v, last_v = values[-1], values[0]
    delta = last_v - first_v
    last = 0.0
    for v in reversed(values):
        if not doubles_equal(v, last) and v < last:
            delta += last
        last = v

    def ts(p: Point) -> float:
        return p.clock + p.ns / 1e9

    end_clock, end_ns = frame.end_ts()
    if frame.period.count is not None:
        span = ts(points[0]) - ts(points[-1])
    else:
        span = float(frame.period.seconds or 0)
    range_end = end_clock + end_ns / 1e9
    range_start = range_end - span
    gap_start = ts(points[-1]) - range_start
    gap_end = range_end - ts(points[0])
    sampled = ts(points[0]) - ts(points[-1])
    average = sampled / (len(points) - 1)
    if delta > 0 and first_v >= 0:
        zero = sampled * (first_v / delta)
        if zero < gap_start:
            gap_start = zero
    threshold = average * 1.1
    interval = sampled
    interval += gap_start if gap_start < threshold else average / 2
    interval += gap_end if gap_end < threshold else average / 2
    return prediction._div(delta * prediction._div(interval, sampled), span)


def _mono(frame: Frame, increasing: bool) -> Any:
    frame.require_numeric()
    if not 1 <= frame.nparams() <= 2:
        raise FunctionError("invalid number of parameters")
    if frame.period.is_empty:
        raise FunctionError("invalid second parameter")
    mode = frame.arg(0, "") or ""
    if mode not in ("", "weak", "strict"):
        raise FunctionError("invalid third parameter")
    strict = mode == "strict"
    values = frame.numbers(frame.values())  # newest first
    if not values:
        raise FunctionError("not enough data")
    eps = DOUBLE_EPSILON if frame.value_type == FLOAT else 0
    for newer, older in zip(values, values[1:]):
        if increasing:
            broken = older >= eps + newer if strict else older > eps + newer
        else:
            broken = older <= -eps + newer if strict else older < -eps + newer
        if broken:
            return 0
    return 1


def fn_monoinc(frame: Frame) -> Any:
    return _mono(frame, True)


def fn_monodec(frame: Frame) -> Any:
    return _mono(frame, False)


def _regression_input(frame: Frame, points: list[Point]) -> tuple[list[float], list[float], float]:
    """t/x arrays and ``now`` exactly as evaluate_FORECAST() builds them."""
    zero = points[-1]  # oldest value
    t = [p.clock - zero.clock + 1.0e-9 * (p.ns - zero.ns + 1) for p in points]
    x = frame.numbers(points)
    now = frame.clock - zero.clock - 1.0e-9 * (zero.ns + 1)
    return t, x, now


def fn_forecast(frame: Frame) -> Any:
    frame.require_numeric()
    nparams = frame.nparams()
    if not 2 <= nparams <= 4:
        raise FunctionError("invalid number of parameters")
    if frame.period.is_empty:
        raise FunctionError("invalid second parameter")
    horizon_text = frame.arg(0)
    if horizon_text is None:
        raise FunctionError("invalid third parameter")
    try:
        horizon = -time_suffix(horizon_text[1:]) if horizon_text.startswith("-") else time_suffix(horizon_text)
    except FunctionError:
        raise FunctionError("invalid third parameter") from None
    try:
        fit, k = prediction.fit_code(frame.arg(1) if nparams >= 3 else "")
        mode = prediction.mode_code(frame.arg(2) if nparams == 4 else "")
    except prediction.PredictionError as exc:
        raise FunctionError(str(exc)) from None
    points = frame.values()
    if not points:
        return MATH_ERROR
    t, x, now = _regression_input(frame, points)
    return prediction.forecast(t, x, now, float(horizon), fit, k, mode)


def fn_timeleft(frame: Frame) -> Any:
    frame.require_numeric()
    nparams = frame.nparams()
    if not 2 <= nparams <= 3:
        raise FunctionError("invalid number of parameters")
    if frame.period.is_empty:
        raise FunctionError("invalid second parameter")
    try:
        threshold = double_suffix(frame.arg(0) or "")
    except FunctionError:
        raise FunctionError("invalid third parameter") from None
    try:
        fit, k = prediction.fit_code(frame.arg(1) if nparams == 3 else "")
    except prediction.PredictionError as exc:
        raise FunctionError(str(exc)) from None
    if fit in (prediction.FIT_EXPONENTIAL, prediction.FIT_POWER) and threshold <= 0.0:
        raise FunctionError("exponential and power functions are always positive")
    points = frame.values()
    if not points:
        return MATH_ERROR
    t, x, now = _regression_input(frame, points)
    return prediction.timeleft(t, x, now, threshold, fit, k)


def _last_log(frame: Frame, max_params: int) -> Point:
    if frame.value_type != LOG:
        raise FunctionError("invalid value type")
    if frame.nparams() > max_params:
        raise FunctionError("invalid number of parameters")
    n = frame.period.count or 1
    points = frame.last_n(n)
    if len(points) < n:
        raise FunctionError("not enough data")
    point = points[n - 1]
    if point.meta is None:
        raise FunctionError("invalid value type")
    return point


def _regexp_match(pattern: str | None, text: str) -> int:
    if not pattern:
        return 1
    if pattern.startswith("@"):
        raise FunctionError(f'global regular expression "{pattern[1:]}" is not available in trigger-tester')
    try:
        return 1 if re.search(pattern, text) else 0
    except re.error:
        raise FunctionError(f'invalid regular expression "{pattern}"') from None


def fn_logeventid(frame: Frame) -> Any:
    point = _last_log(frame, 2)
    return _regexp_match(frame.arg(0), str(point.meta["logeventid"]))


def fn_logsource(frame: Frame) -> Any:
    point = _last_log(frame, 2)
    return _regexp_match(frame.arg(0), str(point.meta["source"] or ""))


def fn_logseverity(frame: Frame) -> Any:
    return _last_log(frame, 1).meta["severity"]


# --- trends -----------------------------------------------------------------


def _trend_range_at(period: Period, clock: int, tz: tzinfo | None) -> tuple[int, int]:
    """zbx_trends_parse_range(): [start, end] hour clocks, end inclusive."""
    if period.num is None or period.unit is None:
        raise FunctionError("invalid second parameter")
    if period.num == 0:
        raise FunctionError("period cannot be zero")
    if period.shift is None:
        raise FunctionError(f'unexpected character[s] in period "{period.raw}"')
    try:
        period.shift.check_min_unit(period.unit)
    except PeriodError as exc:
        raise FunctionError(str(exc)) from None
    end_dt = period.shift.apply_datetime(clock, tz)
    end = int(_add(end_dt, -1, "h").timestamp())
    start = int(_add(end_dt, -period.num, period.unit).timestamp())
    return start, end


def trend_range(frame: Frame) -> tuple[int, int]:
    if frame.nparams() != 1:
        raise FunctionError("invalid number of parameters")
    return _trend_range_at(frame.period, frame.clock, frame.tz)


def _trend_points(frame: Frame):
    frame.require_numeric()
    start, end = trend_range(frame)
    if frame.trends is None:
        raise FunctionError("no trends loaded for item")
    points = frame.trends.between(start, end + 1)
    if not points:
        raise FunctionError("not enough data")
    return points


def fn_trendavg(frame: Frame) -> Any:
    points = _trend_points(frame)
    return sum(p.value_avg * p.num for p in points) / sum(p.num for p in points)


def fn_trendmin(frame: Frame) -> Any:
    return min(p.value_min for p in _trend_points(frame))


def fn_trendmax(frame: Frame) -> Any:
    return max(p.value_max for p in _trend_points(frame))


def fn_trendsum(frame: Frame) -> Any:
    return sum(p.value_avg * p.num for p in _trend_points(frame))


def fn_trendcount(frame: Frame) -> Any:
    return sum(p.num for p in _trend_points(frame))


def fn_trendstl(frame: Frame) -> Any:
    raise FunctionError("trendstl() is only available with the native engine (--engine native)")


# --- baseline (zbxtrends/baseline.c + evaluate_BASELINE) ----------------------

_UNIT_ORDER = "smhdwMy"


def _period_base_unit(period: Period) -> str:
    """zbx_trends_parse_base(): the largest unit used in ``now/<unit>`` cycles of the shift."""
    if period.shift is None:
        raise FunctionError("missing period shift parameter")
    units = [unit for op, _, unit in period.shift.ops if op == "/"]
    if not units:
        raise FunctionError("invalid period shift expression")
    return max(units, key=_UNIT_ORDER.index)


def _trend_avg_state(frame: Frame, start: int, end: int) -> float | None:
    if frame.trends is None:
        return None
    points = frame.trends.between(start, end + 1)
    if not points:
        return None
    return sum(p.value_avg * p.num for p in points) / sum(p.num for p in points)


def _baseline_data(frame: Frame, season_unit: str, season_num: int, skip: int) -> tuple[list[float], list[int]]:
    frame.require_numeric()
    period = frame.period
    period_unit = _period_base_unit(period)
    if season_unit == "M" and period_unit == "w":
        raise FunctionError("weekly data periods cannot be used with month seasons")
    if _UNIT_ORDER.index(season_unit) < _UNIT_ORDER.index(period_unit):
        raise FunctionError("season cannot be less than data period base")
    season_num += 1
    if period_unit == "w" and season_unit == "y":
        raise FunctionError("ISO-year seasons are only available with the native engine")
    values: list[float] = []
    index: list[int] = []
    now = frame.clock
    now_dt = datetime.fromtimestamp(frame.clock, frame.tz)
    for i in range(season_num):
        start, end = _trend_range_at(period, now, frame.tz)
        if skip <= i:
            avg = _trend_avg_state(frame, start, end)
            if avg is None:
                if i == 0:
                    raise FunctionError("not enough data")
            else:
                values.append(avg)
                index.append(i - skip)
        now = int(_add(now_dt, -(i + 1), season_unit).timestamp())
    return values, index


def _baseline_params(frame: Frame) -> tuple[str, int]:
    if frame.nparams() != 3:
        raise FunctionError("invalid number of parameters")
    season_unit = frame.arg(0) or ""
    if season_unit not in ("h", "d", "w", "M", "y"):
        raise FunctionError("invalid third parameter")
    digits = re.match(r"^\s*[+-]?\d+", frame.arg(1) or "")
    season_num = int(digits.group(0)) if digits else 0  # atoi()
    if season_num <= 0:
        raise FunctionError("invalid fourth parameter")
    return season_unit, season_num


def fn_baselinewma(frame: Frame) -> Any:
    season_unit, season_num = _baseline_params(frame)
    values, index = _baseline_data(frame, season_unit, season_num, 1)
    if not values:
        raise FunctionError("not enough data")
    weights = [season_num - i for i in index]
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def fn_baselinedev(frame: Frame) -> Any:
    season_unit, season_num = _baseline_params(frame)
    values, _ = _baseline_data(frame, season_unit, season_num, 0)
    if len(values) <= 1:
        raise FunctionError("not enough data")
    deviation = statistics.pstdev(values)
    if not _is_normal(deviation):
        raise FunctionError("cannot calculate stddevpop() value")
    if deviation < DOUBLE_EPSILON:
        return 0.0
    return abs(values[0] - _mean(values)) / deviation


@dataclass(frozen=True)
class HistorySpec:
    impl: Callable[[Frame], Any]
    period: str = "any"  # 'count' (time period -> #1) | 'seconds' | 'any' | 'none' | 'trend'
    default: str | None = None  # applied when the period is omitted
    required: bool = False
    source: str = "history"  # 'history' | 'trends'
    timer: bool = False  # server re-evaluates such triggers every 30 s


HISTORY_FUNCTIONS: dict[str, HistorySpec] = {
    "last": HistorySpec(fn_last, "count", "#1"),
    "first": HistorySpec(fn_first, "seconds", required=True),
    "min": HistorySpec(fn_min, required=True),
    "max": HistorySpec(fn_max, required=True),
    "avg": HistorySpec(fn_avg, required=True),
    "sum": HistorySpec(fn_sum, required=True),
    "count": HistorySpec(fn_count, "any", "#1"),
    "countunique": HistorySpec(fn_countunique, "any", "#1"),
    "find": HistorySpec(fn_find, "any", "#1"),
    "nodata": HistorySpec(fn_nodata, "seconds", required=True, timer=True),
    "change": HistorySpec(fn_change, "none"),
    "changecount": HistorySpec(fn_changecount, required=True),
    "fuzzytime": HistorySpec(fn_fuzzytime, "seconds", required=True),
    "bitand": HistorySpec(fn_bitand, "count", "#1"),
    "percentile": HistorySpec(fn_percentile, required=True),
    "stddevpop": HistorySpec(_statistical("stddevpop", 1, statistics.pstdev), required=True),
    "stddevsamp": HistorySpec(_statistical("stddevsamp", 2, statistics.stdev), required=True),
    "varpop": HistorySpec(_statistical("varpop", 1, statistics.pvariance), required=True),
    "varsamp": HistorySpec(_statistical("varsamp", 2, statistics.variance), required=True),
    "sumofsquares": HistorySpec(_statistical("sumofsquares", 1, lambda v: sum(x * x for x in v)), required=True),
    "mad": HistorySpec(_statistical("mad", 1, _mad), required=True),
    "skewness": HistorySpec(_statistical("skewness", 1, _skewness), required=True),
    "kurtosis": HistorySpec(_statistical("kurtosis", 1, _kurtosis), required=True),
    "rate": HistorySpec(fn_rate, required=True),
    "monoinc": HistorySpec(fn_monoinc, required=True),
    "monodec": HistorySpec(fn_monodec, required=True),
    "forecast": HistorySpec(fn_forecast, required=True),
    "timeleft": HistorySpec(fn_timeleft, required=True),
    "logeventid": HistorySpec(fn_logeventid, "count", "#1"),
    "logseverity": HistorySpec(fn_logseverity, "count", "#1"),
    "logsource": HistorySpec(fn_logsource, "count", "#1"),
    "trendavg": HistorySpec(fn_trendavg, "trend", required=True, source="trends", timer=True),
    "trendmin": HistorySpec(fn_trendmin, "trend", required=True, source="trends", timer=True),
    "trendmax": HistorySpec(fn_trendmax, "trend", required=True, source="trends", timer=True),
    "trendsum": HistorySpec(fn_trendsum, "trend", required=True, source="trends", timer=True),
    "trendcount": HistorySpec(fn_trendcount, "trend", required=True, source="trends", timer=True),
    "trendstl": HistorySpec(fn_trendstl, "trend", required=True, source="trends", timer=True),
    "baselinewma": HistorySpec(fn_baselinewma, "trend", required=True, source="trends", timer=True),
    "baselinedev": HistorySpec(fn_baselinedev, "trend", required=True, source="trends", timer=True),
}

UNSUPPORTED_HISTORY_FUNCTIONS = {
    "item_count",
    "exists",
    "bucket_rate_foreach",
    "bucket_percentile",
    "histogram_quantile",
}


# ---------------------------------------------------------------------------
# Math / string / date functions (zbxeval built-ins)
# ---------------------------------------------------------------------------


def _num(value: Any, name: str) -> float:
    number = to_number(value)
    if number is None:
        raise FunctionError(f"{name}(): non-numeric argument {value!r}")
    return number


def _nums(values: list[Any], name: str) -> list[float]:
    return [_num(v, name) for v in values]


def _int(value: Any, name: str) -> int:
    number = _num(value, name)
    if isinstance(number, float) and not number.is_integer():
        raise FunctionError(f"{name}(): integer argument expected, got {value!r}")
    return int(number)


def _str(value: Any) -> str:
    return str(value)


def _datetime(clock: int, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(clock, tz)


def math_round(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    return round(_num(args[0], "round"), _int(args[1], "round"))


def math_truncate(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    value, digits = _num(args[0], "truncate"), _int(args[1], "truncate")
    factor = 10**digits
    return math.trunc(value * factor) / factor


def math_between(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    value, low, high = _nums(args, "between")
    return 1 if low <= value <= high else 0


def math_in(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    target, rest = args[0], args[1:]
    if is_number(target):
        for candidate in rest:
            number = to_number(candidate)
            if number is not None and doubles_equal(target, number):
                return 1
        return 0
    return 1 if any(str(candidate) == str(target) for candidate in rest) else 0


def math_log(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    value = _num(args[0], "log")
    if value <= 0:
        raise FunctionError("log(): argument must be positive")
    return math.log(value)


def math_power(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    base, exponent = _nums(args, "power")
    try:
        return base**exponent
    except (OverflowError, ZeroDivisionError) as exc:
        raise FunctionError(f"power(): {exc}") from None


def math_mod(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    dividend, divisor = _nums(args, "mod")
    if divisor == 0:
        raise FunctionError("mod(): division by zero")
    return math.fmod(dividend, divisor)


def math_replace(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    replacement = _str(args[2]) if len(args) > 2 else ""
    return _str(args[0]).replace(_str(args[1]), replacement)


def math_insert(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    text, start, length, replacement = _str(args[0]), _int(args[1], "insert"), _int(args[2], "insert"), _str(args[3])
    return text[: start - 1] + replacement + text[start - 1 + length :]


def math_mid(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
    text, start, length = _str(args[0]), _int(args[1], "mid"), _int(args[2], "mid")
    return text[start - 1 : start - 1 + length]


def _stat(func: Callable[[list[float]], float], name: str) -> Callable:
    def impl(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
        try:
            return func(_nums(args, name))
        except (statistics.StatisticsError, ZeroDivisionError, ValueError) as exc:
            raise FunctionError(f"{name}(): {exc}") from None

    return impl


def _unary(func: Callable[[float], float], name: str) -> Callable:
    def impl(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
        try:
            return func(_num(args[0], name))
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            raise FunctionError(f"{name}(): {exc}") from None

    return impl


def _binary_int(func: Callable[[int, int], int], name: str) -> Callable:
    def impl(args: list[Any], clock: int, tz: tzinfo | None) -> Any:
        return func(_int(args[0], name), _int(args[1], name))

    return impl


@dataclass(frozen=True)
class MathSpec:
    impl: Callable[[list[Any], int, tzinfo | None], Any]
    min_args: int
    max_args: int | None = None  # None = unlimited
    timer: bool = False


MATH_FUNCTIONS: dict[str, MathSpec] = {
    "abs": MathSpec(_unary(abs, "abs"), 1, 1),
    "cbrt": MathSpec(_unary(lambda v: math.copysign(abs(v) ** (1 / 3), v), "cbrt"), 1, 1),
    "ceil": MathSpec(_unary(math.ceil, "ceil"), 1, 1),
    "floor": MathSpec(_unary(math.floor, "floor"), 1, 1),
    "exp": MathSpec(_unary(math.exp, "exp"), 1, 1),
    "expm1": MathSpec(_unary(math.expm1, "expm1"), 1, 1),
    "log": MathSpec(math_log, 1, 1),
    "log10": MathSpec(_unary(math.log10, "log10"), 1, 1),
    "log2": MathSpec(_unary(math.log2, "log2"), 1, 1),
    "sqrt": MathSpec(_unary(math.sqrt, "sqrt"), 1, 1),
    "signum": MathSpec(_unary(lambda v: (v > 0) - (v < 0), "signum"), 1, 1),
    "sin": MathSpec(_unary(math.sin, "sin"), 1, 1),
    "cos": MathSpec(_unary(math.cos, "cos"), 1, 1),
    "tan": MathSpec(_unary(math.tan, "tan"), 1, 1),
    "asin": MathSpec(_unary(math.asin, "asin"), 1, 1),
    "acos": MathSpec(_unary(math.acos, "acos"), 1, 1),
    "atan": MathSpec(_unary(math.atan, "atan"), 1, 1),
    "atan2": MathSpec(lambda a, c, t: math.atan2(_num(a[0], "atan2"), _num(a[1], "atan2")), 2, 2),
    "sinh": MathSpec(_unary(math.sinh, "sinh"), 1, 1),
    "cosh": MathSpec(_unary(math.cosh, "cosh"), 1, 1),
    "cot": MathSpec(_unary(lambda v: 1 / math.tan(v), "cot"), 1, 1),
    "degrees": MathSpec(_unary(math.degrees, "degrees"), 1, 1),
    "radians": MathSpec(_unary(math.radians, "radians"), 1, 1),
    "e": MathSpec(lambda a, c, t: math.e, 0, 0),
    "pi": MathSpec(lambda a, c, t: math.pi, 0, 0),
    "power": MathSpec(math_power, 2, 2),
    "mod": MathSpec(math_mod, 2, 2),
    "round": MathSpec(math_round, 2, 2),
    "truncate": MathSpec(math_truncate, 2, 2),
    "between": MathSpec(math_between, 3, 3),
    "in": MathSpec(math_in, 2, None),
    "rand": MathSpec(lambda a, c, t: 0, 0, 0),
    "avg": MathSpec(_stat(statistics.fmean, "avg"), 1),
    "min": MathSpec(_stat(min, "min"), 1),
    "max": MathSpec(_stat(max, "max"), 1),
    "sum": MathSpec(_stat(sum, "sum"), 1),
    "count": MathSpec(lambda a, c, t: len(a), 1),
    "stddevpop": MathSpec(_stat(statistics.pstdev, "stddevpop"), 1),
    "stddevsamp": MathSpec(_stat(statistics.stdev, "stddevsamp"), 2),
    "varpop": MathSpec(_stat(statistics.pvariance, "varpop"), 1),
    "varsamp": MathSpec(_stat(statistics.variance, "varsamp"), 2),
    "sumofsquares": MathSpec(_stat(lambda v: sum(x * x for x in v), "sumofsquares"), 1),
    "mad": MathSpec(_stat(_mad, "mad"), 1),
    "kurtosis": MathSpec(_stat(_kurtosis, "kurtosis"), 1),
    "skewness": MathSpec(_stat(_skewness, "skewness"), 1),
    "bitand": MathSpec(_binary_int(lambda a, b: a & b, "bitand"), 2, 2),
    "bitor": MathSpec(_binary_int(lambda a, b: a | b, "bitor"), 2, 2),
    "bitxor": MathSpec(_binary_int(lambda a, b: a ^ b, "bitxor"), 2, 2),
    "bitlshift": MathSpec(_binary_int(lambda a, b: a << b, "bitlshift"), 2, 2),
    "bitrshift": MathSpec(_binary_int(lambda a, b: a >> b, "bitrshift"), 2, 2),
    "bitnot": MathSpec(lambda a, c, t: ~_int(a[0], "bitnot") & 0xFFFFFFFFFFFFFFFF, 1, 1),
    "length": MathSpec(lambda a, c, t: len(_str(a[0])), 1, 1),
    "bytelength": MathSpec(lambda a, c, t: len(_str(a[0]).encode()), 1, 1),
    "bitlength": MathSpec(lambda a, c, t: len(_str(a[0]).encode()) * 8, 1, 1),
    "ascii": MathSpec(lambda a, c, t: ord(_str(a[0])[0]) if _str(a[0]) else 0, 1, 1),
    "char": MathSpec(lambda a, c, t: chr(_int(a[0], "char")), 1, 1),
    "concat": MathSpec(lambda a, c, t: "".join(_str(v) for v in a), 2),
    "left": MathSpec(lambda a, c, t: _str(a[0])[: _int(a[1], "left")], 2, 2),
    "right": MathSpec(lambda a, c, t: _str(a[0])[-_int(a[1], "right") :] if _int(a[1], "right") else "", 2, 2),
    "mid": MathSpec(math_mid, 3, 3),
    "trim": MathSpec(lambda a, c, t: _str(a[0]).strip(_str(a[1]) if len(a) > 1 else None), 1, 2),
    "ltrim": MathSpec(lambda a, c, t: _str(a[0]).lstrip(_str(a[1]) if len(a) > 1 else None), 1, 2),
    "rtrim": MathSpec(lambda a, c, t: _str(a[0]).rstrip(_str(a[1]) if len(a) > 1 else None), 1, 2),
    "repeat": MathSpec(lambda a, c, t: _str(a[0]) * _int(a[1], "repeat"), 2, 2),
    "replace": MathSpec(math_replace, 2, 3),
    "insert": MathSpec(math_insert, 4, 4),
    "now": MathSpec(lambda a, c, t: c, 0, 0, timer=True),
    "time": MathSpec(lambda a, c, t: int(_datetime(c, t).strftime("%H%M%S")), 0, 0, timer=True),
    "date": MathSpec(lambda a, c, t: int(_datetime(c, t).strftime("%Y%m%d")), 0, 0, timer=True),
    "dayofweek": MathSpec(lambda a, c, t: _datetime(c, t).isoweekday(), 0, 0, timer=True),
    "dayofmonth": MathSpec(lambda a, c, t: _datetime(c, t).day, 0, 0, timer=True),
}

UNSUPPORTED_MATH_FUNCTIONS = {"jsonpath", "xmlpath", "histogram_quantile"}
