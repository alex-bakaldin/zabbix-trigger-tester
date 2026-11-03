import pytest

from conftest import make_item, make_series
from trigger_tester.expression.ast import HistoryCall, ItemRef
from trigger_tester.expression.functions import HISTORY_FUNCTIONS, Frame, FunctionError
from trigger_tester.expression.period import Period

START = 1_700_000_000


def run(name, series, period, args=(), clock=None, ns=0, trends=None):
    item = series.item if series is not None else make_item()
    spec = HISTORY_FUNCTIONS[name]
    call = HistoryCall(name, ItemRef(item.host, item.key_), period, list(args), f"{name}(...)", item=item)
    call.period = Period.parse(period or "").with_default(spec.default)
    if clock is None:
        clock = series.points[-1].clock if series is not None else START
    return spec.impl(Frame(call, series, trends, clock, ns, None))


@pytest.fixture
def series(float_item):
    # 10 values, one per minute: 1..10
    return make_series(float_item, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])


def test_last(series):
    assert run("last", series, None) == 10
    assert run("last", series, "#3") == 8
    assert run("last", series, "#1:now-5m") == 5
    with pytest.raises(FunctionError):
        run("last", series, "#20")


def test_window_is_left_exclusive(series):
    # 5m window at the last point: (t-300, t] -> values 6..10
    assert run("min", series, "5m") == 6
    assert run("max", series, "5m") == 10
    assert run("avg", series, "5m") == 8
    assert run("sum", series, "#2") == 19
    assert run("first", series, "5m") == 6
    assert run("sum", series, "1s:now-30s") == 0  # empty window: sum() is 0, not an error
    with pytest.raises(FunctionError):
        run("avg", series, "0")  # zero period selects nothing
    assert run("last", series, "5m") == 10  # a time period means last(#1)


def test_evaluation_in_the_middle(series):
    clock = series.points[3].clock  # value 4
    assert run("last", series, None, clock=clock) == 4
    assert run("avg", series, "3m", clock=clock) == 3  # 2,3,4


def test_count_operators(series):
    assert run("count", series, "10m") == 10
    assert run("count", series, "10m", ["gt", "7"]) == 3
    assert run("count", series, "10m", ["le", "2"]) == 2
    assert run("count", series, "#4", ["eq", "9"]) == 1
    assert run("count", series, "10m", ["ge", "1K"]) == 0  # unit suffixes in patterns
    # server rules: no "like"/"bitand" on float items, no operator without a pattern
    for args in (["bitand", "1"], ["like", "1"], ["gt"]):
        with pytest.raises(FunctionError):
            run("count", series, "10m", args)
    assert run("count", series, "10m", ["regexp", "^1"]) == 2  # 1.0000 and 10.0000 as text
    assert run("find", series, "10m", ["gt", "9"]) == 1
