"""Tests for the native (Zabbix server code) engine; skipped when the library is not built."""

import pytest

from conftest import make_item, make_series
from test_evaluator import make_trigger
from trigger_tester.binder import bind
from trigger_tester.expression.evaluator import EvalContext, Evaluator
from trigger_tester.expression.parser import parse_expression
from trigger_tester.history import TrendPoint, TrendSeries
from trigger_tester.native import NativeEngine, NativeEvaluator, NativeParseError, find_library
from trigger_tester.simulator import OK, PROBLEM, Simulator, build_points
from trigger_tester.values import is_unknown

pytestmark = pytest.mark.skipif(find_library() is None, reason="libzbxtester.so is not built")

START = 1_700_000_000


@pytest.fixture
def engine():
    eng = NativeEngine()
    eng.reset()
    yield eng
    eng.reset()


@pytest.fixture
def cpu(engine, float_item):
    series = make_series(float_item, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], start=START)
    engine.load_series(series)
    return series


def test_version(engine):
    assert engine.version.startswith("7.")


def test_functions(engine, cpu):
    last = START + 540
    assert engine.evaluate_function("1", "last", "", last) == ("10", None)
    assert engine.evaluate_function("1", "last", "#3", last) == ("8", None)
    assert engine.evaluate_function("1", "avg", "5m", last)[0] == "8"
    assert engine.evaluate_function("1", "min", "5m:now-5m", last)[0] == "1"
    value, error = engine.evaluate_function("1", "last", "#20", last)
    assert value is None and "not enough data" in error
    # nodata() uses the virtual clock set to the evaluation time
    assert engine.evaluate_function("1", "nodata", "5m", last + 200)[0] == "0"
    assert engine.evaluate_function("1", "nodata", "5m", last + 301)[0] == "1"


def test_expressions(engine, cpu):
    assert engine.evaluate_expression("avg(/srv/cpu,3m)>2 and last(/srv/cpu)=4", START + 180) == (1.0, None)
    assert engine.evaluate_expression("1 or last(/srv/cpu,#50)>0", START) == (1.0, None)
    assert engine.evaluate_expression("0 and last(/srv/cpu,#50)>0", START) == (0.0, None)
    result, error = engine.evaluate_expression("1 and last(/srv/cpu,#50)>0", START)
    assert result is None and "not enough data" in error
    assert engine.evaluate_expression("1K=1024 and 5m=300 and abs(-2)=2", START) == (1.0, None)
    with pytest.raises(NativeParseError):
        engine.evaluate_expression("last(/srv/cpu", START)


def test_trends(engine, float_item, monkeypatch):
    import time

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    day = 1704844800
    trends = TrendSeries(float_item, [TrendPoint(day - 86400 + h * 3600, 60, 1.0, 2.0 + h % 2, 5.0) for h in range(24)])
    engine.load_trends(trends)
    assert engine.evaluate_expression("trendavg(/srv/cpu,1d:now/d)", day + 7200) == (2.5, None)
    assert engine.evaluate_expression("trendmax(/srv/cpu,1d:now/d)", day + 7200) == (5.0, None)


def test_native_evaluator_matches_python(engine):
    item = make_item()
    series = make_series(item, [1, 6, 7, 2, 9, 0, 3], start=START)
    engine.load_series(series)
    text = "last(/srv/cpu)>5 or (change(/srv/cpu)<-5 and 1/last(/srv/cpu)>0)"
    trigger = make_trigger([item])
    binding = bind(parse_expression(text), None, trigger)
    points = build_points([series], START, START + 360, None)

    native = NativeEvaluator(engine)
    native.register(binding.expression, text)
    native_result = Simulator(binding.expression, None, native).run(points)
    python_result = Simulator(binding.expression, None, Evaluator(EvalContext(series={item.itemid: series}))).run(points)

    assert [e.state for e in native_result.evaluations] == [e.state for e in python_result.evaluations]
    assert [e.unknown for e in native_result.evaluations] == [e.unknown for e in python_result.evaluations]
    assert native_result.evaluations[1].functions[0] == ("last(/srv/cpu)", 6)
    assert is_unknown(native_result.evaluations[0].functions[1][1])  # change() has no previous value yet
    assert native_result.evaluations[5].unknown  # division by zero -> UNKNOWN, keeps PROBLEM
    assert native_result.evaluations[5].state == PROBLEM
    assert native_result.evaluations[-1].state == OK


def test_native_only_trend_functions(engine, float_item, monkeypatch):
    import math
    import random
    import time

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    day = 1704844800
    noise = random.Random(1)
    spiky, quiet = [], []
    for h in range(-24 * 28, 3):
        value = 50 + 20 * math.sin(2 * math.pi * (h % 24) / 24) + noise.gauss(0, 1)
        quiet.append(TrendPoint(day + h * 3600, 60, value - 1, value, value + 1))
        value += 40 if h in (1, 2) else 0
        spiky.append(TrendPoint(day + h * 3600, 60, value - 1, value, value + 1))
    engine.load_trends(TrendSeries(float_item, spiky))
    engine.load_trends(TrendSeries(make_item(itemid="2", key="quiet"), quiet))
    rate, error = engine.evaluate_expression("trendstl(/srv/cpu,500h:now/h,2h,24h,3)", day + 3 * 3600)
    assert error is None and rate == 1.0  # both detection hours carry the spike
    rate, error = engine.evaluate_expression("trendstl(/srv/quiet,500h:now/h,24h,24h,3)", day + 3 * 3600)
    assert error is None and 0 <= rate < 0.2
    value, error = engine.evaluate_expression('baselinewma(/srv/cpu,1h:now/h,"d",3)', day + 3 * 3600 + 30)
    assert error is None and 60 < value < 75  # hour 02:00 of the previous three days, without the spike
    _, error = engine.evaluate_expression('baselinewma(/srv/cpu,1w:now/w,"M",1)', day)
    assert "weekly data periods cannot be used with month seasons" in error
