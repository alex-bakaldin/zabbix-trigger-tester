"""Recursive-descent parser for Zabbix 5.4+/7.0 trigger expressions.

Operator precedence (highest first), as documented by Zabbix:
``-`` (unary), ``not``, ``* /``, ``+ -``, ``< <= > >=``, ``= <>``, ``and``, ``or``.

Both the expanded form (``last(/host/key,#1)>0``) and the reduced API form
(``{12345}>0``) are accepted.  History functions are recognised syntactically:
a call whose first argument starts with ``/`` is a history function and its
remaining arguments are kept as raw strings (period, operator, pattern ...).
Any other call is a math/string function whose arguments are expressions.
"""

from __future__ import annotations

import re

from trigger_tester.expression.ast import (
    Binary,
    FuncIdRef,
    HistoryCall,
    ItemRef,
    Macro,
    MathCall,
    Node,
    Number,
    String,
    Unary,
)
from trigger_tester.expression.period import UNIT_SECONDS

SUFFIX_MULTIPLIERS = {
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    **UNIT_SECONDS,
}


class ParseError(ValueError):
    def __init__(self, message: str, text: str, pos: int) -> None:
        super().__init__(f"{message} at position {pos}: {text[:pos]}\u2192{text[pos:]}")
        self.pos = pos
        self.text = text


_NUMBER_RE = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?([KMGTsmhdw])?(?![A-Za-z0-9_.])")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEYWORD_RE = re.compile(r"(and|or|not)(?![A-Za-z0-9_])")
_FUNCID_RE = re.compile(r"\{(\d+)\}")
_MACRO_RE = re.compile(r"\{(\$[A-Z0-9_.]+(?::(?:\"(?:[^\"\\]|\\.)*\"|[^}]*))?|#[A-Z0-9_.]+|[A-Z][A-Z0-9_.]*)\}")
_WS_RE = re.compile(r"\s*")


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    # --- scanner helpers -------------------------------------------------
    def _error(self, message: str, pos: int | None = None) -> ParseError:
        return ParseError(message, self.text, self.pos if pos is None else pos)

    def _skip_ws(self) -> None:
        self.pos = _WS_RE.match(self.text, self.pos).end()

    def _peek(self, n: int = 1) -> str:
        return self.text[self.pos : self.pos + n]

    def _accept(self, token: str) -> bool:
        self._skip_ws()
        if self.text.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def _accept_keyword(self, word: str) -> bool:
        self._skip_ws()
        match = _KEYWORD_RE.match(self.text, self.pos)
        if match and match.group(1) == word:
            self.pos = match.end()
            return True
        return False

    def _expect(self, token: str) -> None:
        if not self._accept(token):
            raise self._error(f"expected {token!r}")

    # --- grammar ---------------------------------------------------------
    def parse(self) -> Node:
        node = self._parse_or()
        self._skip_ws()
        if self.pos != len(self.text):
            raise self._error("unexpected input")
        return node

    def _parse_or(self) -> Node:
        node = self._parse_and()
        while self._accept_keyword("or"):
            node = Binary("or", node, self._parse_and())
        return node

    def _parse_and(self) -> Node:
        node = self._parse_eq()
        while self._accept_keyword("and"):
            node = Binary("and", node, self._parse_eq())
        return node

    def _parse_eq(self) -> Node:
        node = self._parse_rel()
        while True:
            if self._accept("<>"):
                node = Binary("<>", node, self._parse_rel())
            elif self._accept("="):
                node = Binary("=", node, self._parse_rel())
            else:
                return node

    def _parse_rel(self) -> Node:
        node = self._parse_add()
        while True:
            self._skip_ws()
            if self._peek(2) == "<>":
                return node
            for op in ("<=", ">=", "<", ">"):
                if self._accept(op):
                    node = Binary(op, node, self._parse_add())
                    break
            else:
                return node

    def _parse_add(self) -> Node:
        node = self._parse_mul()
        while True:
            if self._accept("+"):
                node = Binary("+", node, self._parse_mul())
            elif self._accept("-"):
                node = Binary("-", node, self._parse_mul())
            else:
                return node

    def _parse_mul(self) -> Node:
        node = self._parse_unary()
        while True:
            if self._accept("*"):
                node = Binary("*", node, self._parse_unary())
            elif self._accept("/"):
                node = Binary("/", node, self._parse_unary())
            else:
                return node

    def _parse_unary(self) -> Node:
        if self._accept_keyword("not"):
            return Unary("not", self._parse_unary())
        if self._accept("-"):
            return Unary("-", self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> Node:
        self._skip_ws()
        if self.pos >= len(self.text):
            raise self._error("unexpected end of expression")
        char = self.text[self.pos]
        if char == "(":
            self.pos += 1
            node = self._parse_or()
            self._expect(")")
            return node
        if char == '"':
            return String(self._read_string())
        if char == "{":
            return self._parse_brace()
        match = _NUMBER_RE.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return Number(_number_value(match.group(0)), match.group(0))
        if _KEYWORD_RE.match(self.text, self.pos):
            raise self._error("unexpected keyword")
        match = _IDENT_RE.match(self.text, self.pos)
        if match:
            name = match.group(0)
            start = self.pos
            self.pos = match.end()
            self._skip_ws()
            if self._peek() != "(":
                raise self._error(f"unknown identifier {name!r}", start)
            self.pos += 1
            return self._parse_call(name, start)
        raise self._error("unexpected character")

    def _parse_brace(self) -> Node:
        match = _FUNCID_RE.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return FuncIdRef(match.group(1))
        match = _MACRO_RE.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return Macro(match.group(0))
        raise self._error("invalid macro or function reference")

    def _parse_call(self, name: str, start: int) -> Node:
        self._skip_ws()
        if self._peek() == "/":
            return self._parse_history_call(name, start)
        args: list[Node] = []
        if self._accept(")"):
            return MathCall(name, args, self.text[start : self.pos])
        while True:
            args.append(self._parse_or())
            if self._accept(","):
                continue
            self._expect(")")
            return MathCall(name, args, self.text[start : self.pos])

    def _parse_history_call(self, name: str, start: int) -> Node:
        self.pos += 1  # '/'
        slash = self.text.find("/", self.pos)
        if slash < 0:
            raise self._error("invalid item reference: missing key")
        host = self.text[self.pos : slash]
        if not host or ")" in host or "," in host:
            raise self._error("invalid item reference: bad host", self.pos)
        self.pos = slash + 1
        key = self._read_until_delimiter().strip()
        if not key:
            raise self._error("invalid item reference: empty key")
        item_filter = None
        if "?[" in key:
            key, _, tail = key.partition("?[")
            if not tail.endswith("]"):
                raise self._error("invalid item filter")
            item_filter = tail[:-1]
        raw_args: list[str] = []
        if self._accept(","):
            raw_args = self._read_raw_args()
        else:
            self._expect(")")
        period = raw_args[0] if raw_args else None
        args = [_convert_raw_arg(a) for a in raw_args[1:]]
        return HistoryCall(
            name=name,
            item_ref=ItemRef(host, key.strip(), item_filter),
            period_raw=period,
            args=args,
            text=self.text[start : self.pos],
            params_raw=",".join(raw_args),
        )

    def _read_until_delimiter(self) -> str:
        """Read raw text up to a depth-0 ``,`` or ``)`` honouring quotes/brackets."""
        start = self.pos
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == '"':
                self._read_string()
                continue
            if char in "([":
                depth += 1
            elif char in ")]":
                if depth == 0:
                    if char == "]":
                        raise self._error("unbalanced ']'")
                    break
                depth -= 1
            elif char == "," and depth == 0:
                break
            self.pos += 1
        if self.pos >= len(self.text):
            raise self._error("unterminated function call", start)
        return self.text[start : self.pos]

    def _read_raw_args(self) -> list[str]:
        args = []
        while True:
            self._skip_ws()
            args.append(self._read_until_delimiter().strip())
            if self._accept(","):
                continue
            self._expect(")")
            return args

    def _read_string(self) -> str:
        assert self.text[self.pos] == '"'
        start = self.pos
        self.pos += 1
        chars = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                if self.pos + 1 >= len(self.text):
                    break
                nxt = self.text[self.pos + 1]
                if nxt in ('"', "\\"):
                    chars.append(nxt)
                    self.pos += 2
                    continue
                chars.append(char)
                self.pos += 1
                continue
            if char == '"':
                self.pos += 1
                return "".join(chars)
            chars.append(char)
            self.pos += 1
        raise self._error("unterminated string", start)


def _number_value(text: str) -> int | float:
    suffix = text[-1] if text[-1] in SUFFIX_MULTIPLIERS else ""
    body = text[:-1] if suffix else text
    if "." in body or "e" in body or "E" in body:
        value: int | float = float(body)
    else:
        value = int(body)
    if suffix:
        value = value * SUFFIX_MULTIPLIERS[suffix]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
    return value


def _convert_raw_arg(raw: str) -> str | None:
    """Raw history-function argument -> unquoted string (or None when empty)."""
    raw = raw.strip()
    if raw == "":
        return None
    if raw.startswith('"'):
        if len(raw) < 2 or not raw.endswith('"'):
            raise ParseError("unterminated string argument", raw, 0)
        return re.sub(r"\\([\"\\])", r"\1", raw[1:-1])
    return raw


def parse_expression(text: str) -> Node:
    text = text.strip()
    if not text:
        raise ParseError("empty expression", text, 0)
    return Parser(text).parse()


def substitute_macros(text: str, macros: dict[str, str]) -> str:
    """Textually replace ``{$NAME}`` (and ``{$NAME:context}``) with given values."""
    if not macros:
        return text

    def repl(match: re.Match) -> str:
        raw = match.group(0)
        inner = match.group(1)
        if raw in macros:
            return macros[raw]
        if inner.startswith("$"):
            base = inner.split(":", 1)[0]
            key = "{" + base + "}"
            if key in macros:
                return macros[key]
        return raw

    return _MACRO_RE.sub(repl, text)
