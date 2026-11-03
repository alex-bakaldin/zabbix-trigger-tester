"""Evaluate a bound expression AST at a given timestamp."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any

from trigger_tester.expression.ast import (
    Binary,
    FuncIdRef,
    HistoryCall,
    Macro,
    MathCall,
    Node,
    Number,
    String,
    Unary,
)
from trigger_tester.expression.functions import (
    HISTORY_FUNCTIONS,
    MATH_FUNCTIONS,
    Frame,
    FunctionError,
)
from trigger_tester.history import Series, TrendSeries
from trigger_tester.values import Unknown, doubles_equal, is_unknown, to_number, truthy


@dataclass
class EvalContext:
    series: dict[str, Series] = field(default_factory=dict)  # itemid -> Series
    trends: dict[str, TrendSeries] = field(default_factory=dict)
    tz: tzinfo | None = None


@dataclass
class Trace:
    """Values of history functions computed during one evaluation."""

    functions: list[tuple[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Evaluator:
    def __init__(self, ctx: EvalContext) -> None:
        self.ctx = ctx

    def evaluate(self, node: Node, clock: int, ns: int, trace: Trace | None = None) -> Any:
        trace = trace if trace is not None else Trace()
        return self._eval(node, clock, ns, trace)

    # ------------------------------------------------------------------
    def _eval(self, node: Node, clock: int, ns: int, trace: Trace) -> Any:
        if isinstance(node, Number):
            return node.value
        if isinstance(node, String):
            return node.value
        if isinstance(node, Macro):
            return self._unknown(trace, f"unresolved macro {node.raw} (use --macro)")
        if isinstance(node, FuncIdRef):
            return self._unknown(trace, f"unbound function reference {{{node.funcid}}}")
        if isinstance(node, Unary):
            return self._eval_unary(node, clock, ns, trace)
        if isinstance(node, Binary):
            return self._eval_binary(node, clock, ns, trace)
        if isinstance(node, HistoryCall):
            return self._eval_history(node, clock, ns, trace)
        if isinstance(node, MathCall):
            return self._eval_math(node, clock, ns, trace)
        return self._unknown(trace, f"cannot evaluate node {type(node).__name__}")

    @staticmethod
    def _unknown(trace: Trace, reason: str) -> Unknown:
        if reason not in trace.errors:
            trace.errors.append(reason)
        return Unknown(reason)

    def _eval_history(self, node: HistoryCall, clock: int, ns: int, trace: Trace) -> Any:
        spec = HISTORY_FUNCTIONS.get(node.name)
        if spec is None:
            return self._unknown(trace, f"unsupported function {node.name}()")
        if node.item is None or node.period is None:
            return self._unknown(trace, f"{node.text}: not bound to an item")
        frame = Frame(
            node,
            self.ctx.series.get(node.item.itemid),
            self.ctx.trends.get(node.item.itemid),
            clock,
            ns,
            self.ctx.tz,
        )
        try:
            value = spec.impl(frame)
        except FunctionError as exc:
            value = self._unknown(trace, f"{node.text}: {exc}")
        except re.error as exc:  # bad regexp in pattern
            value = self._unknown(trace, f"{node.text}: invalid regular expression: {exc}")
        trace.functions.append((node.text, value))
        return value

    def _eval_math(self, node: MathCall, clock: int, ns: int, trace: Trace) -> Any:
        spec = MATH_FUNCTIONS.get(node.name)
        if spec is None:
            return self._unknown(trace, f"unsupported function {node.name}()")
        if len(node.args) < spec.min_args or (spec.max_args is not None and len(node.args) > spec.max_args):
            return self._unknown(trace, f"{node.name}(): invalid number of arguments")
        values = [self._eval(arg, clock, ns, trace) for arg in node.args]
        for value in values:
            if is_unknown(value):
                return value
        try:
            return spec.impl(values, clock, self.ctx.tz)
        except FunctionError as exc:
            return self._unknown(trace, str(exc))
        except (ValueError, TypeError, OverflowError, IndexError) as exc:
            return self._unknown(trace, f"{node.name}(): {exc}")

    def _eval_unary(self, node: Unary, clock: int, ns: int, trace: Trace) -> Any:
        value = self._eval(node.operand, clock, ns, trace)
        if is_unknown(value):
            return value
        if node.op == "not":
            truth = truthy(value)
            if truth is None:
                return self._unknown(trace, f"'not' applied to non-numeric value {value!r}")
            return 0 if truth else 1
        number = to_number(value)
        if number is None:
            return self._unknown(trace, f"unary minus applied to non-numeric value {value!r}")
        return -number

    def _eval_binary(self, node: Binary, clock: int, ns: int, trace: Trace) -> Any:
        left = self._eval(node.left, clock, ns, trace)
        right = self._eval(node.right, clock, ns, trace)
        op = node.op
        if op in ("and", "or"):
            return self._logical(op, left, right, trace)
        if is_unknown(left):
            return left
        if is_unknown(right):
            return right
        if op in ("=", "<>"):
            equal = self._equal(left, right)
            return (1 if equal else 0) if op == "=" else (0 if equal else 1)
        lnum, rnum = to_number(left), to_number(right)
        if lnum is None or rnum is None:
            return self._unknown(trace, f"operator {op!r} needs numeric operands, got {left!r} and {right!r}")
        if op == "<":
            return 1 if lnum < rnum and not doubles_equal(lnum, rnum) else 0
        if op == "<=":
            return 1 if lnum <= rnum or doubles_equal(lnum, rnum) else 0
        if op == ">":
            return 1 if lnum > rnum and not doubles_equal(lnum, rnum) else 0
        if op == ">=":
            return 1 if lnum >= rnum or doubles_equal(lnum, rnum) else 0
        if op == "+":
            return lnum + rnum
        if op == "-":
            return lnum - rnum
        if op == "*":
            return lnum * rnum
        if op == "/":
            if rnum == 0:
                return self._unknown(trace, "division by zero")
            return lnum / rnum
        return self._unknown(trace, f"unknown operator {op!r}")

    @staticmethod
    def _equal(left: Any, right: Any) -> bool:
        if isinstance(left, str) and isinstance(right, str):
            return left == right
        lnum, rnum = to_number(left), to_number(right)
        if lnum is None or rnum is None:
            return str(left) == str(right)
        return doubles_equal(lnum, rnum)

    def _logical(self, op: str, left: Any, right: Any, trace: Trace) -> Any:
        """Zabbix three-valued logic: 1 or UNKNOWN = 1, 0 and UNKNOWN = 0."""
        truths = []
        for value in (left, right):
            if is_unknown(value):
                truths.append(None)
                continue
            truth = truthy(value)
            if truth is None:
                return self._unknown(trace, f"{op!r} applied to non-numeric value {value!r}")
            truths.append(truth)
        if op == "and":
            if False in truths:
                return 0
            if None in truths:
                return left if is_unknown(left) else right
            return 1
        if True in truths:
            return 1
        if None in truths:
            return left if is_unknown(left) else right
        return 0
