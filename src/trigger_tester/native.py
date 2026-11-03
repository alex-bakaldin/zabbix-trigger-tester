"""ctypes bridge to libzbxtester.so: the real Zabbix server expression engine.

See native/README.md for how the library is built.  When the library is not
available the tester falls back to the pure-Python implementation in
``trigger_tester.expression`` with reduced fidelity.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trigger_tester.expression.ast import HistoryCall, Node, walk
from trigger_tester.expression.evaluator import Trace
from trigger_tester.history import Series, TrendSeries
from trigger_tester.values import Unknown, to_number

LIB_NAME = "libzbxtester.so"
ENV_VAR = "ZBX_TRIGGER_TESTER_LIB"


class NativeError(Exception):
    pass


class NativeParseError(NativeError):
    pass


def candidate_paths() -> list[Path]:
    paths = []
    if os.environ.get(ENV_VAR):
        paths.append(Path(os.environ[ENV_VAR]))
    here = Path(__file__).resolve().parent
    paths.append(here / LIB_NAME)
    paths.append(here.parent.parent / "native" / LIB_NAME)
    return paths


def find_library() -> Path | None:
    for path in candidate_paths():
        if path.is_file():
            return path
    return None


class NativeEngine:
    """Thin wrapper around the exported zt_* functions."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        resolved = Path(path) if path else find_library()
        if resolved is None:
            raise NativeError(f"{LIB_NAME} not found (set {ENV_VAR} or build it, see native/README.md)")
        try:
            lib = ctypes.CDLL(str(resolved))
        except OSError as exc:
            raise NativeError(f"cannot load {resolved}: {exc}") from None
        self.path = resolved
        self.lib = lib
        c_str, c_int, c_u64 = ctypes.c_char_p, ctypes.c_int, ctypes.c_uint64
        lib.zt_zabbix_version.restype = c_str
        lib.zt_zabbix_version.argtypes = []
        lib.zt_item_register.argtypes = [c_u64, c_int, c_str, c_str]
        lib.zt_item_add_value.argtypes = [c_u64, c_int, c_int, c_str, c_int, c_str, c_int, c_int]
        lib.zt_item_add_trend.argtypes = [c_u64, c_int, c_int, ctypes.c_double, ctypes.c_double, ctypes.c_double]
        lib.zt_item_finish.argtypes = [c_u64]
        lib.zt_evaluate_function.argtypes = [c_u64, c_str, c_str, c_int, c_int, ctypes.POINTER(c_str), ctypes.POINTER(c_str)]
        lib.zt_evaluate_expression.argtypes = [c_str, c_int, c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(c_str)]
        lib.zt_free.argtypes = [ctypes.c_void_p]
        lib.zt_reset.argtypes = []
        lib.zt_set_debug.argtypes = [c_int]
        lib.zt_set_now.argtypes = [c_int, c_int]
        self.version = lib.zt_zabbix_version().decode()
        self.source_tag = "unknown"
        if hasattr(lib, "zt_source_tag"):
            lib.zt_source_tag.restype = c_str
            lib.zt_source_tag.argtypes = []
            self.source_tag = lib.zt_source_tag().decode()
        self._items: set[str] = set()

    def describe(self) -> str:
        return f"Zabbix {self.version} server code, tag {self.source_tag} ({self.path})"

    def set_debug(self, enabled: bool) -> None:
        self.lib.zt_set_debug(1 if enabled else 0)

    def reset(self) -> None:
        self.lib.zt_reset()
        self._items.clear()

    def _take(self, pointer: ctypes.c_char_p) -> str | None:
        value = pointer.value
        self.lib.zt_free(pointer)
        return value.decode(errors="replace") if value is not None else None

    # --- data ------------------------------------------------------------
    def load_series(self, series: Series) -> None:
        item = series.item
        itemid = int(item.itemid)
        if item.itemid not in self._items:
            if self.lib.zt_item_register(itemid, item.value_type, item.host.encode(), item.key_.encode()) != 0:
                raise NativeError(f"cannot register item {item.ref}")
            self._items.add(item.itemid)
        for point in series.points:
            meta = point.meta or {}
            rc = self.lib.zt_item_add_value(
                itemid,
                point.clock,
                point.ns,
                str(point.value).encode(),
                int(meta.get("timestamp", 0)),
                str(meta.get("source") or "").encode(),
                int(meta.get("severity", 0)),
                int(meta.get("logeventid", 0)),
            )
            if rc != 0:
                raise NativeError(f"cannot add value for item {item.ref}")
        self.lib.zt_item_finish(itemid)

    def load_trends(self, trends: TrendSeries) -> None:
        item = trends.item
        itemid = int(item.itemid)
        if item.itemid not in self._items:
            self.lib.zt_item_register(itemid, item.value_type, item.host.encode(), item.key_.encode())
            self._items.add(item.itemid)
        for t in trends.points:
            self.lib.zt_item_add_trend(itemid, t.clock, t.num, t.value_min, t.value_avg, t.value_max)

    # --- evaluation -----------------------------------------------------------
    def evaluate_function(self, itemid: str, name: str, params: str, clock: int, ns: int = 0) -> tuple[str | None, str | None]:
        out, err = ctypes.c_char_p(), ctypes.c_char_p()
        rc = self.lib.zt_evaluate_function(int(itemid), name.encode(), params.encode(), clock, ns, ctypes.byref(out), ctypes.byref(err))
        value, error = self._take(out), self._take(err)
        return (value, None) if rc == 0 else (None, error or "unknown error")

    def evaluate_expression(self, text: str, clock: int, ns: int = 0) -> tuple[float | None, str | None]:
        result, err = ctypes.c_double(), ctypes.c_char_p()
        rc = self.lib.zt_evaluate_expression(text.encode(), clock, ns, ctypes.byref(result), ctypes.byref(err))
        error = self._take(err)
        if rc == 2:
            raise NativeParseError(error or "cannot parse expression")
        return (result.value, None) if rc == 0 else (None, error or "unknown error")


@dataclass
class NativeEvaluator:
    """Drop-in replacement for ``Evaluator`` backed by the server engine.

    Expressions are evaluated from their text; the AST is used only to list the
    history functions whose individual values are recorded in the trace.
    """

    engine: NativeEngine
    texts: dict[int, str] = field(default_factory=dict)  # id(node) -> expression text
    calls: dict[int, list[HistoryCall]] = field(default_factory=dict)

    def register(self, node: Node, text: str) -> None:
        self.texts[id(node)] = text
        self.calls[id(node)] = [n for n in walk(node) if isinstance(n, HistoryCall)]

    def check(self, node: Node, clock: int) -> None:
        """Raise NativeParseError early if the engine rejects the expression."""
        self.engine.evaluate_expression(self.texts[id(node)], clock, 0)

    def evaluate(self, node: Node, clock: int, ns: int, trace: Trace | None = None) -> Any:
        trace = trace if trace is not None else Trace()
        text = self.texts.get(id(node))
        if text is None:
            raise NativeError("expression node was not registered with the native evaluator")
        for call in self.calls[id(node)]:
            if call.item is None:
                continue
            value, error = self.engine.evaluate_function(call.item.itemid, call.name, call.params_raw, clock, ns)
            if error is not None:
                trace.functions.append((call.text, Unknown(error)))
            else:
                trace.functions.append((call.text, _convert(value, call.item.is_numeric)))
        result, error = self.engine.evaluate_expression(text, clock, ns)
        if error is not None:
            if error not in trace.errors:
                trace.errors.append(error)
            return Unknown(error)
        return result


def _convert(text: str | None, numeric_item: bool) -> Any:
    """Server returns values as text; numbers are converted back when they parse."""
    if text is None:
        return None
    number = to_number(text)
    return number if number is not None else text
