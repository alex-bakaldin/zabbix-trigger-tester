"""AST nodes for Zabbix trigger expressions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trigger_tester.expression.period import Period


class Node:
    pass


@dataclass
class Number(Node):
    value: int | float
    text: str = ""


@dataclass
class String(Node):
    value: str


@dataclass
class Macro(Node):
    """Unresolved ``{$MACRO}`` / ``{HOST.*}`` / ``{#LLD}`` reference."""

    raw: str


@dataclass
class Unary(Node):
    op: str  # 'not' | '-'
    operand: Node


@dataclass
class Binary(Node):
    op: str  # or and = <> < <= > >= + - * /
    left: Node
    right: Node


@dataclass
class ItemRef:
    host: str
    key: str
    filter: str | None = None

    def __str__(self) -> str:
        text = f"/{self.host}/{self.key}"
        if self.filter:
            text += f"?[{self.filter}]"
        return text


@dataclass
class HistoryCall(Node):
    """History / trend function: ``name(/host/key, period, args...)``.

    ``funcid`` is set when the call came from the reduced ``{12345}`` form;
    the binder fills ``item`` and ``period`` from the API data in that case.
    """

    name: str
    item_ref: ItemRef | None
    period_raw: str | None
    args: list[str | None]
    text: str
    params_raw: str = ""  # everything after the item reference, e.g. '5m,"gt","10"'
    funcid: str | None = None
    # Filled by the binder:
    item: Any = None  # trigger_tester.model.Item
    period: Period | None = None


@dataclass
class MathCall(Node):
    name: str
    args: list[Node]
    text: str = ""


@dataclass
class FuncIdRef(Node):
    """``{12345}`` placeholder from a reduced trigger expression."""

    funcid: str


def walk(node: Node):
    """Yield every node of the tree (pre-order)."""
    yield node
    if isinstance(node, Unary):
        yield from walk(node.operand)
    elif isinstance(node, Binary):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, MathCall):
        for arg in node.args:
            yield from walk(arg)


def to_text(node: Node, top: bool = True) -> str:
    """Render a node back to Zabbix syntax (used for reports)."""
    if isinstance(node, Number):
        return node.text or str(node.value)
    if isinstance(node, String):
        return '"' + node.value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(node, Macro):
        return node.raw
    if isinstance(node, Unary):
        inner = to_text(node.operand, False)
        return f"not {inner}" if node.op == "not" else f"-{inner}"
    if isinstance(node, Binary):
        text = f"{to_text(node.left, False)} {node.op} {to_text(node.right, False)}"
        return text if top else f"({text})"
    if isinstance(node, HistoryCall):
        return node.text
    if isinstance(node, MathCall):
        return node.text or f"{node.name}({', '.join(to_text(a) for a in node.args)})"
    if isinstance(node, FuncIdRef):
        return "{" + node.funcid + "}"
    return repr(node)
