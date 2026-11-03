"""Bind parsed expressions to Zabbix items and work out what data to preload."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from trigger_tester.expression.ast import (
    Binary,
    FuncIdRef,
    HistoryCall,
    ItemRef,
    MathCall,
    Node,
    Unary,
    walk,
)
from trigger_tester.expression.functions import (
    HISTORY_FUNCTIONS,
    MATH_FUNCTIONS,
    UNSUPPORTED_HISTORY_FUNCTIONS,
    UNSUPPORTED_MATH_FUNCTIONS,
)
from trigger_tester.expression.parser import ParseError, Parser
from trigger_tester.expression.period import UNIT_MAX_SECONDS, Period, PeriodError
from trigger_tester.history import ItemRequirements
from trigger_tester.model import Item, Trigger

ItemResolver = Callable[[str, str], Item | None]


class BindError(Exception):
    pass


@dataclass
class Binding:
    expression: Node
    recovery: Node | None
    items: dict[str, Item] = field(default_factory=dict)
    requirements: dict[str, ItemRequirements] = field(default_factory=dict)
    has_timer_functions: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def history_calls(self) -> list[HistoryCall]:
        calls = [n for n in walk(self.expression) if isinstance(n, HistoryCall)]
        if self.recovery is not None:
            calls += [n for n in walk(self.recovery) if isinstance(n, HistoryCall)]
        return calls


def bind(
    expression: Node,
    recovery: Node | None,
    trigger: Trigger,
    resolver: ItemResolver | None = None,
) -> Binding:
    expression = _replace_funcids(expression, trigger)
    recovery = _replace_funcids(recovery, trigger) if recovery is not None else None
    binding = Binding(expression, recovery)
    for node in binding.history_calls:
        _bind_call(node, trigger, resolver, binding)
    for root in (expression, recovery):
        if root is None:
            continue
        for node in walk(root):
            if isinstance(node, MathCall):
                spec = MATH_FUNCTIONS.get(node.name)
                if spec is None:
                    if node.name in UNSUPPORTED_MATH_FUNCTIONS:
                        raise BindError(f"function {node.name}() is not supported by the tester")
                    if node.name in HISTORY_FUNCTIONS:
                        raise BindError(f"{node.text}: first argument must be an item reference /host/key")
                    raise BindError(f"unknown function {node.name}()")
                if spec.timer:
                    binding.has_timer_functions = True
    return binding


def _replace_funcids(node: Node, trigger: Trigger) -> Node:
    """Turn reduced ``{functionid}`` references into HistoryCall nodes."""
    if isinstance(node, FuncIdRef):
        function = trigger.functions.get(node.funcid)
        if function is None:
            raise BindError(f"function reference {{{node.funcid}}} is not part of trigger {trigger.triggerid}")
        item = trigger.items.get(function.itemid)
        if item is None:
            raise BindError(f"item {function.itemid} of function {{{node.funcid}}} is not available")
        params = _split_parameters(function.parameter)
        if params and params[0] == "$":
            params = params[1:]
        period = params[0] if params else None
        args = [_unquote(p) for p in params[1:]]
        text = f"{function.name}({item.ref}" + "".join("," + p for p in params) + ")"
        return HistoryCall(function.name, ItemRef(item.host, item.key_), period, args, text, params_raw=",".join(params),
                funcid=node.funcid, item=item)
    if isinstance(node, Unary):
        node.operand = _replace_funcids(node.operand, trigger)
    elif isinstance(node, Binary):
        node.left = _replace_funcids(node.left, trigger)
        node.right = _replace_funcids(node.right, trigger)
    elif isinstance(node, MathCall):
        node.args = [_replace_funcids(a, trigger) for a in node.args]
    return node


def _split_parameters(parameter: str) -> list[str]:
    if not parameter.strip():
        return []
    parser = Parser(parameter + ")")
    try:
        return parser._read_raw_args()  # noqa: SLF001 - shares the quoting rules
    except ParseError as exc:
        raise BindError(f"cannot parse function parameters {parameter!r}: {exc}") from None


def _unquote(raw: str) -> str | None:
    from trigger_tester.expression.parser import _convert_raw_arg

    return _convert_raw_arg(raw)


def _bind_call(node: HistoryCall, trigger: Trigger, resolver: ItemResolver | None, binding: Binding) -> None:
    spec = HISTORY_FUNCTIONS.get(node.name)
    if spec is None:
        if node.name in UNSUPPORTED_HISTORY_FUNCTIONS or node.name.endswith("_foreach"):
            raise BindError(f"{node.text}: function {node.name}() is not supported by the tester")
        raise BindError(f"{node.text}: unknown function {node.name}()")

    if node.item is None:
        ref = node.item_ref
        assert ref is not None
        if ref.filter is not None or ref.host == "*" or "*" in ref.key:
            raise BindError(f"{node.text}: wildcard / filtered item references are not supported")
        item = trigger.item_by_ref(ref.host, ref.key)
        if item is None and resolver is not None:
            item = resolver(ref.host, ref.key)
        if item is None:
            raise BindError(f"{node.text}: item /{ref.host}/{ref.key} not found")
        node.item = item
    item = node.item

    try:
        period = Period.parse(node.period_raw or "")
    except PeriodError as exc:
        raise BindError(f"{node.text}: {exc}") from None
    if spec.period == "none":
        if not period.is_empty or period.shift is not None:
            raise BindError(f"{node.text}: {node.name}() takes no period")
    elif spec.period == "trend":
        if period.num is None or period.shift is None:
            raise BindError(f"{node.text}: {node.name}() expects a period like 1d:now/d")
        try:
            period.shift.check_min_unit(period.unit or "s")
        except PeriodError as exc:
            raise BindError(f"{node.text}: {exc}") from None
    else:
        if period.is_calendar:
            raise BindError(f"{node.text}: invalid time period {period.raw!r}")
        period = period.with_default(spec.default)
        if spec.required and period.is_empty:
            raise BindError(f"{node.text}: {node.name}() requires a period")
        if spec.period == "count" and period.seconds is not None:
            # the server defaults a time period to last(#1) for last()-style functions
            period = Period(period.raw, count=1, shift=period.shift)
        if spec.period == "seconds" and period.count is not None:
            raise BindError(f"{node.text}: {node.name}() expects a time period")
    node.period = period

    binding.items[item.itemid] = item
    req = binding.requirements.setdefault(item.itemid, ItemRequirements())
    req.functions.append(node.text)
    shift_back = period.shift.max_backward_seconds() if period.shift else 0
    if spec.source == "trends":
        req.needs_trends = True
        lookback = period.lookback_seconds() + 3600
        if node.name.startswith("baseline") and len(node.args) >= 2:
            unit = (node.args[0] or "h") if node.args[0] in UNIT_MAX_SECONDS else "h"
            seasons = int(node.args[1]) if (node.args[1] or "").isdigit() else 1
            lookback += (seasons + 1) * UNIT_MAX_SECONDS[unit]
        req.trend_lookback_seconds = max(req.trend_lookback_seconds, lookback)
    else:
        req.needs_history = True
        if period.seconds is not None:
            req.merge_seconds(period.seconds + shift_back)
        else:
            req.merge_count(period.count or 1)
            if shift_back:
                req.merge_seconds(shift_back)
        if node.name == "change":
            req.merge_count(2)
    if spec.timer or period.shift is not None:
        binding.has_timer_functions = True
    if not item.is_numeric and spec.source == "trends":
        binding.warnings.append(f"{node.text}: trend functions need a numeric item, {item.ref} is {item.value_type_name}")
