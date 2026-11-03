from conftest import make_item, make_series
from test_evaluator import make_trigger
from trigger_tester.binder import bind
from trigger_tester.expression.evaluator import EvalContext, Evaluator
from trigger_tester.expression.parser import parse_expression
from trigger_tester.simulator import OK, PROBLEM, Simulator, build_points

START = 1_700_000_000


def simulate(expr, values, recovery=None, recovery_mode=0, multiple=False, initial=OK, step=None):
    item = make_item()
    series = make_series(item, values, start=START)
    trigger = make_trigger([item])
    binding = bind(parse_expression(expr), parse_expression(recovery) if recovery else None, trigger)
    evaluator = Evaluator(EvalContext(series={item.itemid: series}))
    points = build_points([series], START, START + 60 * (len(values) - 1), step)
    sim = Simulator(binding.expression, binding.recovery, evaluator, recovery_mode, multiple, initial)
    return sim.run(points)


def states(result):
    return [ev.state for ev in result.evaluations]


def test_expression_recovery_mode():
    result = simulate("last(/srv/cpu)>5", [1, 6, 7, 2, 9])
    assert states(result) == [OK, PROBLEM, PROBLEM, OK, PROBLEM]
    assert result.problem_events == 2 and result.ok_events == 1
    assert [ev.clock for ev in result.transitions] == [START + 60, START + 180, START + 240]
    assert result.problem_intervals(START, START + 240) == [(START + 60, START + 180), (START + 240, None)]


def test_recovery_expression_mode():
    # problem above 5, recover only below 3 (hysteresis)
    result = simulate("last(/srv/cpu)>5", [1, 6, 4, 4, 2, 4], recovery="last(/srv/cpu)<3", recovery_mode=1)
    assert states(result) == [OK, PROBLEM, PROBLEM, PROBLEM, OK, OK]


def test_no_recovery_mode():
    result = simulate("last(/srv/cpu)>5", [6, 1, 1], recovery_mode=2)
    assert states(result) == [PROBLEM, PROBLEM, PROBLEM]


def test_multiple_problem_events():
    result = simulate("last(/srv/cpu)>5", [6, 7, 8], multiple=True)
    assert result.problem_events == 3 and len(result.transitions) == 1


def test_unknown_keeps_state():
    # division by zero at the second point -> UNKNOWN, previous PROBLEM state is kept
    result = simulate("last(/srv/cpu)<10 and 1/last(/srv/cpu)>0", [5, 0, 1])
    evs = result.evaluations
    assert [ev.state for ev in evs] == [PROBLEM, PROBLEM, PROBLEM]
    assert evs[1].unknown and "division by zero" in evs[1].errors
    assert result.unknown_count == 1 and result.problem_events == 1


def test_three_valued_logic_in_trigger():
    # change() is UNKNOWN at the first point, but "0 and UNKNOWN" is 0 -> a defined OK
    result = simulate("last(/srv/cpu)>5 and change(/srv/cpu)>0", [1, 7])
    assert not result.evaluations[0].unknown and result.evaluations[0].state == OK
    assert result.evaluations[1].state == PROBLEM
    # "UNKNOWN or 1" is 1
    result = simulate("change(/srv/cpu)>0 or last(/srv/cpu)>5", [7])
    assert not result.evaluations[0].unknown and result.evaluations[0].state == PROBLEM


def test_initial_problem_state():
    result = simulate("last(/srv/cpu)>5", [1, 1], initial=PROBLEM)
    assert result.initial_state == PROBLEM
    assert result.transitions[0].state == OK and result.ok_events == 1


def test_timer_points():
    result = simulate("nodata(/srv/cpu,90s)=1", [1, 1, 1], step=30)
    sources = {ev.source for ev in result.evaluations}
    assert sources == {"value", "timer"}
    assert len(result.evaluations) == 3 + 2  # values + timer points in seconds without values
    assert all(ev.state == OK for ev in result.evaluations)
