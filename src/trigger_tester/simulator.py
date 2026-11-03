"""Replay a trigger's state machine over a list of evaluation points."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from trigger_tester.expression.ast import Node
from trigger_tester.expression.evaluator import Evaluator, Trace
from trigger_tester.history import MAX_NS, Series
from trigger_tester.values import is_unknown, truthy

OK = "OK"
PROBLEM = "PROBLEM"

RECOVERY_EXPRESSION_MODE = 0
RECOVERY_RECOVERY_EXPRESSION = 1
RECOVERY_NONE = 2


@dataclass(frozen=True)
class EvalPoint:
    clock: int
    ns: int
    source: str  # 'value' | 'timer'
    itemids: tuple[str, ...] = ()


@dataclass
class Evaluation:
    clock: int
    ns: int
    source: str
    itemids: tuple[str, ...]
    value: Any
    recovery_value: Any
    state: str
    unknown: bool
    errors: list[str]
    functions: list[tuple[str, Any]]
    event: str | None  # 'PROBLEM' | 'OK' | None
    changed: bool


@dataclass
class SimulationResult:
    initial_state: str
    evaluations: list[Evaluation] = field(default_factory=list)
    transitions: list[Evaluation] = field(default_factory=list)
    problem_events: int = 0
    ok_events: int = 0
    unknown_count: int = 0
    error_counter: Counter = field(default_factory=Counter)

    @property
    def final_state(self) -> str:
        return self.evaluations[-1].state if self.evaluations else self.initial_state

    def problem_intervals(self, time_from: int, time_till: int) -> list[tuple[int, int | None]]:
        intervals: list[tuple[int, int | None]] = []
        start: int | None = time_from if self.initial_state == PROBLEM else None
        for ev in self.transitions:
            if ev.state == PROBLEM and start is None:
                start = ev.clock
            elif ev.state == OK and start is not None:
                intervals.append((start, ev.clock))
                start = None
        if start is not None:
            intervals.append((start, None))
        return intervals


def build_points(series_list: list[Series], time_from: int, time_till: int, step: int | None) -> list[EvalPoint]:
    """Evaluation moments: every new item value in the period plus an optional timer grid."""
    by_key: dict[tuple[int, int], set[str]] = {}
    for series in series_list:
        for point in series.in_range(time_from, time_till):
            by_key.setdefault(point.key, set()).add(series.item.itemid)
    points = [EvalPoint(clock, ns, "value", tuple(sorted(ids))) for (clock, ns), ids in by_key.items()]
    if step and step > 0:
        value_clocks = {clock for clock, _ in by_key}
        for clock in range(time_from, time_till + 1, step):
            if clock not in value_clocks:
                points.append(EvalPoint(clock, MAX_NS, "timer"))
    points.sort(key=lambda p: (p.clock, p.ns))
    return points


class Simulator:
    def __init__(
        self,
        expression: Node,
        recovery: Node | None,
        evaluator: Evaluator,
        recovery_mode: int = RECOVERY_EXPRESSION_MODE,
        multiple_problem_events: bool = False,
        initial_state: str = OK,
    ) -> None:
        self.expression = expression
        self.recovery = recovery
        self.evaluator = evaluator
        self.recovery_mode = recovery_mode
        self.multiple = multiple_problem_events
        self.initial_state = initial_state
        if recovery_mode == RECOVERY_RECOVERY_EXPRESSION and recovery is None:
            raise ValueError("recovery expression mode requires a recovery expression")

    def run(self, points: list[EvalPoint]) -> SimulationResult:
        result = SimulationResult(initial_state=self.initial_state)
        state = self.initial_state
        for point in points:
            evaluation = self._evaluate_point(point, state)
            if evaluation.unknown:
                result.unknown_count += 1
                for error in evaluation.errors:
                    result.error_counter[error] += 1
            if evaluation.event == PROBLEM:
                result.problem_events += 1
            elif evaluation.event == OK:
                result.ok_events += 1
            if evaluation.changed:
                result.transitions.append(evaluation)
            state = evaluation.state
            result.evaluations.append(evaluation)
        return result

    def _evaluate_point(self, point: EvalPoint, state: str) -> Evaluation:
        trace = Trace()
        value = self.evaluator.evaluate(self.expression, point.clock, point.ns, trace)
        truth = self._truth(value, trace, "expression")

        recovery_value: Any = None
        recovery_truth: bool | None = None
        if self.recovery_mode == RECOVERY_RECOVERY_EXPRESSION and self.recovery is not None:
            recovery_value = self.evaluator.evaluate(self.recovery, point.clock, point.ns, trace)
            recovery_truth = self._truth(recovery_value, trace, "recovery expression")

        new_state = state
        event: str | None = None
        unknown = truth is None or (
            self.recovery_mode == RECOVERY_RECOVERY_EXPRESSION and recovery_truth is None
        )
        if not unknown:
            if truth:
                if state == OK or self.multiple:
                    event = PROBLEM
                new_state = PROBLEM
            elif state == PROBLEM:
                if self.recovery_mode == RECOVERY_EXPRESSION_MODE or (
                    self.recovery_mode == RECOVERY_RECOVERY_EXPRESSION and recovery_truth
                ):
                    event = OK
                    new_state = OK
        return Evaluation(
            clock=point.clock,
            ns=point.ns,
            source=point.source,
            itemids=point.itemids,
            value=value,
            recovery_value=recovery_value,
            state=new_state,
            unknown=unknown,
            errors=list(trace.errors),
            functions=list(trace.functions),
            event=event,
            changed=new_state != state,
        )

    @staticmethod
    def _truth(value: Any, trace: Trace, what: str) -> bool | None:
        if is_unknown(value):
            return None
        truth = truthy(value)
        if truth is None:
            reason = f"{what} evaluated to non-numeric value {value!r}"
            if reason not in trace.errors:
                trace.errors.append(reason)
            return None
        return truth
