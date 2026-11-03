"""Parsing of raw API payloads (shapes taken from a live Zabbix 8.0 server)."""

from trigger_tester.api import ZabbixAPI


def test_get_trigger_parses_live_payload(monkeypatch):
    rows = {
        "trigger.get": [
            [
                {
                    "triggerid": "29442",
                    "description": "Zabbix agent is not available",
                    "expression": "max({53109},3m)=0",
                    "recovery_mode": "0",
                    "recovery_expression": "",
                    "type": "0",
                    "status": "0",
                    "state": "0",
                    "value": "1",
                    "priority": "3",
                    "hosts": [{"hostid": "10642", "host": "tuf", "name": "tuf"}],
                    "items": [
                        {
                            "itemid": "62639",
                            "hostid": "10642",
                            "name": "Zabbix agent availability",
                            "key_": "zabbix[host,agent,available]",
                            "value_type": "3",
                            "delay": "1m",
                            "units": "",
                            "history": "7d",
                            "trends": "365d",
                            "status": "0",
                            "state": "0",
                        }
                    ],
                    # the live API calls the field "function", not "name"
                    "functions": [
                        {"functionid": "53109", "itemid": "62639", "triggerid": "29442", "parameter": "$,{$AGENT.TIMEOUT}", "function": "max"}
                    ],
                },
            ],
            [
                {
                    "triggerid": "29442",
                    "expression": "max(/tuf/zabbix[host,agent,available],3m)=0",
                    "recovery_expression": "",
                    "description": "Zabbix agent is not available",
                }
            ],
        ]
    }

    def fake_call(method, params=None, auth=True):
        return rows[method].pop(0)

    api = ZabbixAPI("http://example")
    monkeypatch.setattr(api, "call", fake_call)
    trigger = api.get_trigger("29442")

    assert trigger.functions["53109"].name == "max"
    assert trigger.functions["53109"].parameter == "$,{$AGENT.TIMEOUT}"
    assert trigger.expanded_expression == "max(/tuf/zabbix[host,agent,available],3m)=0"
    assert trigger.items["62639"].host == "tuf"


def test_history_is_fetched_in_bounded_chunks(monkeypatch):
    from conftest import make_item

    item = make_item(itemid="7")
    start = 1_700_000_000
    # 20 000 values, one every 3 s: 16.7 h, i.e. 3 six-hour windows and several pages per window
    clocks = [start + 3 * i for i in range(20_000)]
    requests_seen = []

    def fake_call(method, params=None, auth=True):
        assert method == "history.get"
        requests_seen.append(params)
        rows = [c for c in clocks if params["time_from"] <= c <= params["time_till"]]
        return [{"itemid": "7", "clock": str(c), "ns": "0", "value": "1"} for c in rows[: params["limit"]]]

    api = ZabbixAPI("http://example", page_size=2500, chunk_seconds=6 * 3600)
    monkeypatch.setattr(api, "call", fake_call)
    points = api.get_history(item, start, clocks[-1])

    assert sorted(p.clock for p in points) == clocks
    assert all(p["time_till"] - p["time_from"] < 6 * 3600 for p in requests_seen)
    assert all(p["limit"] == 2500 for p in requests_seen)
    assert 9 <= len(requests_seen) <= 12  # 3 windows, pages overlap by one row at the cursor

    # chunking off: one window, still paged
    requests_seen.clear()
    api = ZabbixAPI("http://example", page_size=2500, chunk_seconds=0)
    monkeypatch.setattr(api, "call", fake_call)
    assert len(api.get_history(item, start, clocks[-1])) == 20_000
    assert requests_seen[0]["time_till"] == clocks[-1] and len(requests_seen) == 9
