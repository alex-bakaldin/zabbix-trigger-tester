from conftest import make_item, make_series
from test_evaluator import make_trigger
from trigger_tester.binder import bind
from trigger_tester.expression.evaluator import EvalContext, Evaluator
from trigger_tester.expression.parser import parse_expression
from trigger_tester.history import Point, Series
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


# --- recalculation rules of zbx_evaluate_expressions() -----------------------------------------

def _two_item_setup(cpu_values, mem_values, expr, recovery, recovery_mode=1):
    cpu = make_item(itemid="100", key="cpu")
    mem = make_item(itemid="200", key="mem")
    cpu_series = Series(cpu, [Point(START + off, 0, float(v), None) for off, v in cpu_values])
    mem_series = Series(mem, [Point(START + off, 0, float(v), None) for off, v in mem_values])
    trigger = make_trigger([cpu, mem])
    binding = bind(parse_expression(expr), parse_expression(recovery) if recovery else None, trigger)
    evaluator = Evaluator(EvalContext(series={cpu.itemid: cpu_series, mem.itemid: mem_series}))
    return binding, evaluator, [cpu_series, mem_series]


def _sim(binding, evaluator, recovery_mode=1, initial=OK):
    return Simulator(
        binding.expression, binding.recovery, evaluator, recovery_mode, False, initial,
        problem_itemids=binding.problem_itemids,
        timer_in_expression=binding.timer_in_expression,
        timer_in_recovery=binding.timer_in_recovery,
    )


def test_binding_knows_problem_items_and_timers():
    binding, _, _ = _two_item_setup([(0, 1)], [(0, 1)], "last(/srv/cpu)>5", "nodata(/srv/mem,60s)=1")
    assert binding.problem_itemids == {"100"}
    assert not binding.timer_in_expression and binding.timer_in_recovery and binding.has_timer_functions
    binding, _, _ = _two_item_setup([(0, 1)], [(0, 1)], "last(/srv/cpu,#1:now-1h)>5", None)
    assert not binding.has_timer_functions  # a time shift is not a time-based function
    binding, _, _ = _two_item_setup([(0, 1)], [(0, 1)], "time()>0", None)
    assert binding.timer_in_expression and binding.timer_seed == int(make_trigger([]).triggerid)


def test_recovery_only_item_cannot_raise_problem():
    # cpu is already high, but the period starts with a value of mem, which is only in the recovery expression
    binding, evaluator, series = _two_item_setup([(0, 9)], [(30, 0)], "last(/srv/cpu)>5", "last(/srv/mem)<1")
    points = build_points(series, START + 30, START + 30, None)
    assert [p.itemids for p in points] == [("200",)]
    result = _sim(binding, evaluator).run(points)
    ev = result.evaluations[0]
    assert ev.state == OK and ev.event is None and ev.suppressed and result.suppressed_count == 1
    # the same point with a permissive simulator (no item information) raises PROBLEM
    permissive = Simulator(binding.expression, binding.recovery, evaluator, 1, False, OK)
    assert permissive.run(points).evaluations[0].state == PROBLEM


def test_recovery_item_can_recover():
    binding, evaluator, series = _two_item_setup(
        [(0, 9), (120, 1)], [(60, 5), (180, 0)], "last(/srv/cpu)>5", "last(/srv/mem)<1"
    )
    points = build_points(series, START, START + 180, None)
    result = _sim(binding, evaluator).run(points)
    assert states(result) == [PROBLEM, PROBLEM, PROBLEM, OK]
    assert result.evaluations[1].recovery_value is None  # expression true: recovery not consulted
    assert result.evaluations[2].recovery_value == 0.0 and result.evaluations[3].recovery_value == 1.0


def test_recovery_unknown_only_matters_when_consulted():
    # recovery divides by the value: UNKNOWN at 0, but only consulted when the expression is false in PROBLEM
    result = simulate("last(/srv/cpu)>5", [6, 0, 1], recovery="1/last(/srv/cpu)>0", recovery_mode=1)
    evs = result.evaluations
    assert [ev.state for ev in evs] == [PROBLEM, PROBLEM, OK]
    assert [ev.unknown for ev in evs] == [False, True, False]
    result = simulate("last(/srv/cpu)>5", [0], recovery="1/last(/srv/cpu)>0", recovery_mode=1)
    assert not result.evaluations[0].unknown  # OK state: recovery expression is not evaluated


def test_timer_points_only_when_server_would_schedule_them():
    # no time-based function in the problem expression: timer points run only while in PROBLEM
    binding, evaluator, series = _two_item_setup([(0, 9), (30, 1)], [], "last(/srv/cpu)>5", "nodata(/srv/cpu,45s)=1")
    points = build_points(series, START, START + 150, 30)
    result = _sim(binding, evaluator).run(points)
    assert [(ev.clock - START, ev.source, ev.state) for ev in result.evaluations] == [
        (0, "value", PROBLEM),
        (30, "value", PROBLEM),  # expression false, nodata 0 -> stays
        (60, "timer", PROBLEM),  # 45 s window still holds the value from +30
        (90, "timer", OK),  # nodata -> recovered
    ]  # 120 and 150 are not evaluated: OK state, no timer in the problem expression
    # with the timer in the problem expression every grid point is evaluated and may raise PROBLEM
    binding, evaluator, series = _two_item_setup([(0, 1)], [], "nodata(/srv/cpu,45s)=1", None)
    result = _sim(binding, evaluator, recovery_mode=0).run(build_points(series, START, START + 90, 30))
    assert [(ev.clock - START, ev.state) for ev in result.evaluations] == [(0, OK), (30, OK), (60, PROBLEM), (90, PROBLEM)]


def test_timer_grid_offset():
    series = [make_series(make_item(), [1], start=START)]
    points = build_points(series, START, START + 120, 30, offset=1234567)
    timers = [p.clock for p in points if p.source == "timer"]
    assert timers and all(c % 30 == 1234567 % 30 for c in timers) and timers[0] >= START
