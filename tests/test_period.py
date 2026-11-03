from zoneinfo import ZoneInfo

import pytest

from trigger_tester.expression.period import Period, PeriodError, TimeShift, parse_duration

UTC = ZoneInfo("UTC")


def test_parse_duration():
    assert parse_duration("300") == 300
    assert parse_duration("5m") == 300
    assert parse_duration("1d") == 86400
    with pytest.raises(PeriodError):
        parse_duration("5x")


def test_period_count_and_seconds():
    assert Period.parse("#5").count == 5
    assert Period.parse("10m").seconds == 600
    p = Period.parse("1h:now-1d")
    assert p.seconds == 3600 and p.shift.raw == "now-1d"
    assert Period.parse("").is_empty
    assert Period.parse("").with_default("#1").count == 1
    with pytest.raises(PeriodError):
        Period.parse("#0")


def test_time_shift_apply():
    # 2024-01-10 13:45:30 UTC (Wednesday)
    t = 1704894330
    assert TimeShift.parse("now-1h").apply(t, UTC) == t - 3600
    assert TimeShift.parse("now/h").apply(t, UTC) == 1704891600
    assert TimeShift.parse("now/d").apply(t, UTC) == 1704844800
    assert TimeShift.parse("now/w").apply(t, UTC) == 1704672000  # Monday 2024-01-08
    assert TimeShift.parse("now/M").apply(t, UTC) == 1704067200  # 2024-01-01
    assert TimeShift.parse("now/d-1d").apply(t, UTC) == 1704758400
    assert TimeShift.parse("now-1M/d").apply(t, UTC) == 1702166400  # 2023-12-10


def test_time_shift_lookback():
    assert Period.parse("1d:now/d").lookback_seconds() == 2 * 86400
    assert Period.parse("5m").lookback_seconds() == 300
