import pytest

from trigger_tester.expression.ast import HistoryCall, MathCall, Macro, FuncIdRef, to_text, walk
from trigger_tester.expression.parser import ParseError, parse_expression, substitute_macros


def calls(node):
    return [n for n in walk(node) if isinstance(n, HistoryCall)]


def test_precedence_and_or_not():
    node = parse_expression("1 or 0 and not 1")
    assert to_text(node) == "1 or (0 and not 1)"


def test_precedence_arith_vs_compare():
    node = parse_expression("1+2*3>6=1")
    assert to_text(node) == "((1 + (2 * 3)) > 6) = 1"


def test_number_suffixes():
    assert parse_expression("1K").value == 1024
    assert parse_expression("1.5M").value == 1.5 * 1024 * 1024
    assert parse_expression("5m").value == 300
    assert parse_expression("1e3").value == 1000.0
    assert parse_expression("-2").op == "-"


def test_history_call_parts():
    node = parse_expression('count(/Zabbix server/vfs.fs.size[/,pfree],10m:now-1h,"gt","10")>=3')
    call = calls(node)[0]
    assert call.name == "count"
    assert call.item_ref.host == "Zabbix server"
    assert call.item_ref.key == "vfs.fs.size[/,pfree]"
    assert call.period_raw == "10m:now-1h"
    assert call.args == ["gt", "10"]
    assert call.text == 'count(/Zabbix server/vfs.fs.size[/,pfree],10m:now-1h,"gt","10")'


def test_history_call_without_period():
    call = calls(parse_expression("change(/h/k)>0"))[0]
    assert call.period_raw is None and call.args == []


def test_key_with_nested_brackets_and_quotes():
    call = calls(parse_expression('last(/h/web.test[a,"x,y)",[b]])="ok"'))[0]
    assert call.item_ref.key == 'web.test[a,"x,y)",[b]]'


def test_item_filter_is_extracted():
    call = calls(parse_expression('sum(last_foreach(/*/k?[tag="x"]))>1'))[0]
    assert call.item_ref.host == "*" and call.item_ref.filter == 'tag="x"'


def test_math_call_with_nested_history():
    node = parse_expression("abs(change(/h/k))>1 and dayofweek()<6")
    math_calls = [n for n in walk(node) if isinstance(n, MathCall)]
    assert {m.name for m in math_calls} == {"abs", "dayofweek"}
    assert len(calls(node)) == 1


def test_string_escapes():
    node = parse_expression('last(/h/k)="a\\"b\\\\c"')
    assert node.right.value == 'a"b\\c'


def test_macros_and_funcids():
    node = parse_expression("{12345}>{$THRESHOLD} and {$M:\"ctx\"}=1")
    assert isinstance(node.left.left, FuncIdRef) and node.left.left.funcid == "12345"
    assert isinstance(node.left.right, Macro) and node.left.right.raw == "{$THRESHOLD}"
    assert node.right.left.raw == '{$M:"ctx"}'


@pytest.mark.parametrize(
    "text",
    ["", "last(/h/k", "1 +", "foo", "last(/h)", "1 == 2", "(1", '"abc', "5min"],
)
def test_parse_errors(text):
    with pytest.raises(ParseError):
        parse_expression(text)


def test_substitute_macros():
    text = "last(/h/k,{$N})>{$T:\"ctx\"} and {$X}=1"
    out = substitute_macros(text, {"{$N}": "#3", "{$T}": "5", "{$X}": "2"})
    assert out == "last(/h/k,#3)>5 and 2=1"
