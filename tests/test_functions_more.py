import pytest

from conftest import make_item, make_series
from test_functions import run, START
from trigger_tester.expression.functions import DBL_MAX, FunctionError
from trigger_tester.history import Point, Series, TrendPoint, TrendSeries


def test_count_bitand_result_mask():
    s = make_series(make_item(itemid="3", key="bits", value_type=3), [2, 6, 10, 3])
    # pattern is "<result>/<mask>": (value & mask) == result
    assert run("count", s, "10m", ["bitand", "2/3"]) == 3
    assert run("count", s, "10m", ["bitand", "3/2"]) == 0
    assert run("count", s, "10m", ["bitand", "2"]) == 4
    assert run("bitand", s, "#1", ["2"]) == 2
    assert run("bitand", s, "#2", ["8"]) == 8
    assert run("changecount", s, "10m") == 3
    assert run("changecount", s, "10m", ["dec"]) == 1
    assert run("changecount", s, "#2", ["inc"]) == 0


def test_string_functions(text_item):
    s = make_series(text_item, ["ok", "error: disk", "ERROR", "ok"])
    assert run("count", s, "10m", ["like", "rror"]) == 1  # case-sensitive
    assert run("count", s, "10m", ["iregexp", "^err"]) == 2
    assert run("count", s, "10m", ["eq", "ok"]) == 2
    assert run("find", s, "#2", ["like", "ERROR"]) == 1
    assert run("find", s, None, ["like", "ERROR"]) == 0
    assert run("countunique", s, "10m") == 3
    assert run("last", s, None) == "ok"
    assert run("change", s, None) == 1


def test_nodata(float_item):
    s = make_series(float_item, [1, 2, 3])
    last = s.points[-1].clock
    assert run("nodata", s, "5m", clock=last + 200) == 0
    assert run("nodata", s, "5m", clock=last + 301) == 1
    assert run("nodata", None, "5m") == 1


def test_change_and_fuzzytime(float_item):
    s = make_series(float_item, [5, 9])
    assert run("change", s, None) == 4
    clock_item = make_item(itemid="9", key="system.localtime")
    t = Series(clock_item, [Point(START, 0, START - 30)])
    assert run("fuzzytime", t, "60", clock=START) == 1
    assert run("fuzzytime", t, "10", clock=START) == 0


def test_statistics(float_item):
    s = make_series(float_item, [2, 4, 4, 4, 5, 5, 7, 9])
    assert run("stddevpop", s, "10m") == 2
    assert run("percentile", s, "10m", ["50"]) == 4
    assert run("percentile", s, "10m", ["100"]) == 9
    assert run("sumofsquares", s, "#2") == 130
    assert run("mad", s, "10m") == 0.5
    assert run("monoinc", s, "10m") == 1
    assert run("monoinc", s, "10m", ["strict"]) == 0
    assert run("monodec", s, "10m") == 0


def test_rate_with_counter_reset(float_item):
    s = make_series(float_item, [100, 160, 220, 20], interval=60)
    # evaluate_RATE(): increase 140 (with the reset), extrapolated like Prometheus over the 10m range
    assert run("rate", s, "10m") == pytest.approx(0.2722222222)
    assert run("rate", s, "10m", clock=s.points[-1].clock + 360) == pytest.approx(0.35)


def test_timeleft_and_forecast(float_item):
    # grows 1 per minute: at the last point value is 10, threshold 100 -> 90 min
    s = make_series(float_item, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert run("timeleft", s, "10m", ["100"]) == pytest.approx(90 * 60)
    assert run("forecast", s, "10m", ["1h"]) == pytest.approx(70)
    flat = make_series(float_item, [5, 5, 5, 5])
    assert run("timeleft", flat, "10m", ["100"]) == DBL_MAX
    assert run("timeleft", s, "10m", ["0"]) == DBL_MAX  # moving away from threshold
    assert run("timeleft", s, "10m", ["100", "polynomial2"]) == pytest.approx(90 * 60, rel=1e-6)
    assert run("forecast", s, "10m", ["1h", "polynomial2"]) == pytest.approx(70, rel=1e-6)
    assert run("forecast", s, "10m", ["1h", "exponential"]) == pytest.approx(12886362.892417155, rel=1e-6)
    assert run("forecast", s, "10m", ["1h", "logarithmic"]) == pytest.approx(run("forecast", s, "10m", ["1h", "logarithmic", "value"]))
    assert run("forecast", s, "10m", ["1h", "", "delta"]) == pytest.approx(60, rel=1e-6)
    assert run("forecast", s, "10m", ["1h", "polynomial3", "max"]) == pytest.approx(70, rel=1e-4)
    assert run("forecast", s, "1s:now-30s", ["1h"]) == -1.0  # empty window: ZBX_MATH_ERROR, not UNKNOWN
    with pytest.raises(FunctionError):
        run("timeleft", s, "10m", ["0", "exponential"])


def test_log_functions():
    item = make_item(itemid="7", key="log[/var/log/syslog]", value_type=2)
    points = [
        Point(START, 0, "boot", {"timestamp": START, "source": "kernel", "severity": 2, "logeventid": 100}),
        Point(START + 60, 0, "fail", {"timestamp": START, "source": "sshd", "severity": 4, "logeventid": 4625}),
    ]
    s = Series(item, points)
    assert run("logseverity", s, None) == 4
    assert run("logsource", s, None, ["^ssh"]) == 1
    assert run("logeventid", s, None, ["^4625$"]) == 1
    assert run("logeventid", s, "#2", ["^4625$"]) == 0


def test_trend_functions(float_item):
    from zoneinfo import ZoneInfo

    day = 1704844800  # 2024-01-10 00:00 UTC
    trends = TrendSeries(
        float_item,
        [TrendPoint(day - 86400 + h * 3600, 60, 1.0, 2.0 + h % 2, 5.0) for h in range(24)]
        + [TrendPoint(day + 3600, 60, 0.0, 9.0, 9.0)],
    )
    from trigger_tester.expression.ast import HistoryCall, ItemRef
    from trigger_tester.expression.functions import HISTORY_FUNCTIONS, Frame
    from trigger_tester.expression.period import Period

    def trend(name):
        call = HistoryCall(name, ItemRef("srv", "cpu"), "1d:now/d", [], name, item=float_item)
        call.period = Period.parse("1d:now/d")
        return HISTORY_FUNCTIONS[name].impl(Frame(call, None, trends, day + 7200, 0, ZoneInfo("UTC")))

    assert trend("trendavg") == 2.5
    assert trend("trendmin") == 1.0
    assert trend("trendmax") == 5.0
    assert trend("trendcount") == 24 * 60
    assert trend("trendsum") == 2.5 * 24 * 60


def test_baseline_and_trendstl(float_item):
    from zoneinfo import ZoneInfo

    from trigger_tester.expression.ast import HistoryCall, ItemRef
    from trigger_tester.expression.functions import HISTORY_FUNCTIONS, Frame
    from trigger_tester.expression.period import Period

    day = 1704844800  # Wed 2024-01-10 00:00 UTC
    trends = TrendSeries(
        float_item,
        [TrendPoint(day + h * 3600, 60, 0, 10 + (h % 24) + 100 * ((h // 24) % 7 == 2), 0) for h in range(-24 * 35, 3)],
    )

    def run_trend(name, period, args, clock=day + 2 * 3600 + 30):
        call = HistoryCall(name, ItemRef("srv", "cpu"), period, list(args), name, item=float_item)
        call.period = Period.parse(period)
        return HISTORY_FUNCTIONS[name].impl(Frame(call, None, trends, clock, 0, ZoneInfo("UTC")))

    assert run_trend("baselinewma", "1h:now/h", ["d", "3"]) == 11.0
    assert run_trend("baselinedev", "1h:now/h", ["d", "3"]) == 0.0
    assert run_trend("baselinewma", "1d:now/d", ["w", "2"]) == 21.5
    dec = [p for p in trends.points if 1701388800 <= p.clock < 1704067200]  # December 2023 hours
    assert run_trend("trendavg", "1M:now/M", []) == pytest.approx(sum(p.value_avg for p in dec) / len(dec), rel=1e-9)
    assert run_trend("trendcount", "1M:now/M", []) == len(dec) * 60
    with pytest.raises(FunctionError, match="fourth"):
        run_trend("baselinewma", "1h:now/h", ["d", "0"])
    with pytest.raises(FunctionError, match="weekly"):
        run_trend("baselinewma", "1w:now/w", ["M", "1"])
    with pytest.raises(FunctionError, match="native"):
        run_trend("trendstl", "500h:now/h", ["2h", "24h"])
