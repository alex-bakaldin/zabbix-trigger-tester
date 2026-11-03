import pytest

from conftest import make_series
from trigger_tester.binder import bind
from trigger_tester.expression.evaluator import EvalContext, Evaluator, Trace
from trigger_tester.expression.parser import parse_expression
from trigger_tester.model import Trigger
from trigger_tester.values import is_unknown


def make_trigger(items, functions=None):

    return Trigger(
        triggerid="1",
        description="t",
        expression="",
        recovery_expression="",
        expanded_expression="",
        expanded_recovery_expression="",
        recovery_mode=0,
        type=0,
        priority=3,
        status=0,
        items={i.itemid: i for i in items},
        hosts={i.hostid: i.host for i in items},
        functions={f.functionid: f for f in (functions or [])},
    )


def evaluate(text, series=None, clock=None, tz=None, items=None, trigger=None):
    series = series or []
    items = items or [s.item for s in series]
    trigger = trigger or make_trigger(items)
    binding = bind(parse_expression(text), None, trigger)
    ctx = EvalContext(series={s.item.itemid: s for s in series}, tz=tz)
    if clock is None:
        clock = series[0].points[-1].clock if series else 1_700_000_000
    trace = Trace()
    return Evaluator(ctx).evaluate(binding.expression, clock, 0, trace), trace


def test_arithmetic_and_comparison():
    assert evaluate("1+2*3")[0] == 7
    assert evaluate("(1+2)*3=9")[0] == 1
    assert evaluate("10/4>2")[0] == 1
    assert evaluate("1K=1024 and 5m=300")[0] == 1
    assert evaluate("0.1+0.2=0.3")[0] == 1  # epsilon comparison
    assert evaluate("1<>1")[0] == 0
    assert evaluate("-(2)+3")[0] == 1
    assert evaluate("not 0")[0] == 1


def test_division_by_zero_unknown():
    value, trace = evaluate("1/0>1")
    assert is_unknown(value) and "division by zero" in trace.errors[0]


def test_three_valued_logic():
    assert evaluate("1 or 1/0")[0] == 1
    assert evaluate("0 and 1/0")[0] == 0
    assert is_unknown(evaluate("1 and 1/0")[0])
    assert is_unknown(evaluate("0 or 1/0")[0])
    assert is_unknown(evaluate("not (1/0)")[0])


def test_string_comparison(text_item):
    s = make_series(text_item, ["up", "down"])
    assert evaluate('last(/srv/status)="down"', [s])[0] == 1
    assert evaluate('last(/srv/status)<>"up"', [s])[0] == 1
    value, trace = evaluate('last(/srv/status)>1', [s])
    assert is_unknown(value)


def test_history_in_expression(float_item):
    s = make_series(float_item, [10, 20, 30, 40])
    value, trace = evaluate("avg(/srv/cpu,3m)>20 and last(/srv/cpu)=40", [s])
    assert value == 1
    assert dict(trace.functions) == {"avg(/srv/cpu,3m)": 30, "last(/srv/cpu)": 40}


def test_math_functions_and_time(float_item):
    from zoneinfo import ZoneInfo

    s = make_series(float_item, [10, 7])
    assert evaluate("abs(change(/srv/cpu))=3", [s])[0] == 1
    assert evaluate("min(last(/srv/cpu),5)=5", [s])[0] == 1
    assert evaluate("between(last(/srv/cpu),1,10)", [s])[0] == 1
    assert evaluate('in(last(/srv/cpu),1,7)', [s])[0] == 1
    assert evaluate('length("abc")+bitand(12,10)', [s])[0] == 11
    # 2024-01-10 13:45:30 UTC, Wednesday
    assert evaluate("dayofweek()", clock=1704894330, tz=ZoneInfo("UTC"))[0] == 3
    assert evaluate("time()", clock=1704894330, tz=ZoneInfo("UTC"))[0] == 134530
    assert evaluate("date()", clock=1704894330, tz=ZoneInfo("UTC"))[0] == 20240110
    assert evaluate("now()", clock=1704894330)[0] == 1704894330


def test_unresolved_macro_is_unknown():
    value, trace = evaluate("{$X}>1")
    assert is_unknown(value) and "{$X}" in trace.errors[0]


def test_reduced_form_binding(float_item):
    from trigger_tester.model import TriggerFunction

    s = make_series(float_item, [1, 2, 3])
    trigger = make_trigger([float_item], [TriggerFunction("77", "1", "avg", "$,2m"), TriggerFunction("78", "1", "count", '$,5m,"gt","1"')])
    value, trace = evaluate("{77}>2 and {78}=2", [s], trigger=trigger)
    assert value == 1
    assert [t for t, _ in trace.functions] == ["avg(/srv/cpu,2m)", 'count(/srv/cpu,5m,"gt","1")']


def test_bind_errors(float_item):
    from trigger_tester.binder import BindError

    s = make_series(float_item, [1])
    with pytest.raises(BindError, match="not found"):
        evaluate("last(/other/key)>1", [s])
    with pytest.raises(BindError, match="requires a period"):
        evaluate("avg(/srv/cpu)>1", [s])
    with pytest.raises(BindError, match="unknown function"):
        evaluate("foo(/srv/cpu,1m)>1", [s])
    with pytest.raises(BindError, match="not supported"):
        evaluate("sum(last_foreach(/*/cpu))>1", [s])
