"""End-to-end run of the CLI against a fake Zabbix API."""

import json

import pytest

import trigger_tester.cli as cli
from trigger_tester.native import find_library
from trigger_tester.history import Point
from trigger_tester.model import Item, Trigger, TriggerFunction

START = 1_700_000_000


class FakeAPI:
    instances = []

    def __init__(self, url, token=None, verify=True, timeout=60, **options):
        self.url = url
        self.token = token
        self.calls = []
        self.item = Item("100", "10", "srv1", "CPU", "system.cpu.util", 0, "1m")
        self.values = [10, 20, 95, 97, 30, 10, 99, 5]
        FakeAPI.instances.append(self)

    def version(self):
        return "7.0.5"

    def login(self, username, password):
        self.token = "session"

    def logout(self):
        self.calls.append("logout")

    def get_trigger(self, triggerid):
        assert triggerid == "555"
        return Trigger(
            triggerid="555",
            description="High CPU on srv1",
            expression="{1}>{$CPU.MAX}",
            recovery_expression="",
            expanded_expression="max(/srv1/system.cpu.util,2m)>90",
            expanded_recovery_expression="",
            recovery_mode=0,
            type=0,
            priority=4,
            status=0,
            items={"100": self.item},
            hosts={"10": "srv1"},
            functions={"1": TriggerFunction("1", "100", "max", "$,2m")},
        )

    def find_item(self, host, key):
        self.calls.append(("find_item", host, key))
        return self.get_trigger("555").item_by_ref(host, key)

    def get_history(self, item, time_from, time_till):
        self.calls.append(("history", time_from, time_till))
        return [Point(START + i * 60, 0, float(v)) for i, v in enumerate(self.values) if time_from <= START + i * 60 <= time_till]

    def get_history_before(self, item, before_clock, count):
        self.calls.append(("before", before_clock, count))
        return [Point(START - 60, 0, 5.0)]

    def get_trends(self, item, time_from, time_till):
        return []

    def get_trigger_events(self, triggerid, time_from, time_till):
        return [
            {"eventid": "1", "clock": START + 120, "ns": 0, "value": 1, "name": "High CPU"},
            {"eventid": "2", "clock": START + 240, "ns": 0, "value": 0, "name": "High CPU"},
            {"eventid": "3", "clock": START + 300, "ns": 0, "value": 0, "name": "High CPU"},  # not reproducible
        ]

    def get_last_event_before(self, triggerid, before_clock):
        return {"eventid": "0", "clock": START - 1000, "value": 0}


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    FakeAPI.instances.clear()
    monkeypatch.setattr(cli, "ZabbixAPI", FakeAPI)
    yield


def run_cli(*extra):
    argv = ["--url", "http://zbx", "--token", "x", "--from", str(START), "--till", str(START + 7 * 60), "--tz", "UTC"]
    if not any(a == "--expression" for a in extra):
        argv += ["-t", "555"]
    if not any(a == "--engine" for a in extra):
        argv += ["--engine", "python"]
    return cli.main(argv + list(extra))


@pytest.mark.parametrize("engine", ["python", pytest.param("native", marks=pytest.mark.skipif(find_library() is None, reason="libzbxtester.so is not built"))])
def test_end_to_end(capsys, tmp_path, engine):
    out_file = tmp_path / "res.json"
    code = run_cli("--compare-events", "--json", str(out_file), "--engine", engine)
    out = capsys.readouterr().out
    assert code == 0
    assert "Trigger #555: High CPU on srv1" in out
    assert "max(/srv1/system.cpu.util,2m) > 90" in out
    assert "PROBLEM  max(/srv1/system.cpu.util,2m)=95" in out
    assert "problem intervals: 2" in out
    assert "matched: 2, missed: 1, extra: 1" in out
    data = json.loads(out_file.read_text())
    assert data["final_state"] == "PROBLEM"
    assert ("Zabbix 7" in out) == (engine == "native")
    assert [e["state"] for e in data["evaluations"]] == ["OK", "OK", "PROBLEM", "PROBLEM", "PROBLEM", "OK", "PROBLEM", "PROBLEM"]
    # lookback: 2m window -> history requested from START-120, plus count-based prefetch
    api = FakeAPI.instances[0]
    assert ("history", START - 120, START + 420) in api.calls
    assert "logout" in api.calls


def test_adhoc_expression_and_verbose(capsys):
    code = run_cli("--expression", "last(/srv1/system.cpu.util)>{$LIMIT}", "--macro", "LIMIT=50", "-v", "--step", "0")
    out = capsys.readouterr().out
    assert code == 0
    assert "Ad-hoc expression (no trigger)" in out and "hosts: srv1" in out and "recovery mode: expression" in out
    assert "last(/srv1/system.cpu.util) > 50" in out
    assert out.count("\n*") == 4  # OK->P, P->OK, OK->P, P->OK
    api = FakeAPI.instances[0]
    assert ("find_item", "srv1", "system.cpu.util") in api.calls
    assert not any(c[0] == "trigger" for c in api.calls if isinstance(c, tuple))


def test_adhoc_recovery_expression(capsys, tmp_path):
    out_file = tmp_path / "res.json"
    code = run_cli("--expression", "last(/srv1/system.cpu.util)>90", "--recovery-expression", "last(/srv1/system.cpu.util)<10",
                   "--step", "0", "--json", str(out_file))
    out = capsys.readouterr().out
    assert code == 0
    assert "recovery mode: recovery expression" in out
    data = json.loads(out_file.read_text())
    assert data["trigger"]["triggerid"] is None and data["trigger"]["recovery_mode"] == 1
    assert data["trigger"]["recovery_expression"] == "last(/srv1/system.cpu.util) < 10"


def test_trigger_and_expression_are_exclusive(capsys):
    base = ["--url", "http://zbx", "--token", "x"]
    assert cli.main(base + ["-t", "1", "--expression", "last(/a/b)>1"]) == 2
    assert "not both" in capsys.readouterr().err
    assert cli.main(base) == 2
    assert cli.main(base + ["-t", "1", "--recovery-expression", "last(/a/b)<1"]) == 2
    assert cli.main(base + ["--expression", "last(/a/b)>1", "--compare-events"]) == 2


def test_unresolved_macro_reported(capsys):
    code = run_cli("--expression", "last(/srv1/system.cpu.util)>{$LIMIT}")
    out = capsys.readouterr().out
    assert code == 0
    assert "unresolved macro {$LIMIT}" in out
    assert "unknown: 8" in out


def test_missing_connection_args(capsys):
    code = cli.main(["-t", "1"])
    assert code == 2
    assert "--url" in capsys.readouterr().err
