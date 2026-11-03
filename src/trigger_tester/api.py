"""Minimal Zabbix JSON-RPC client (Zabbix 7.0+, bearer token authentication)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import requests

from trigger_tester.history import Point, TrendPoint, point_from_api
from trigger_tester.model import Item, Trigger, TriggerFunction

log = logging.getLogger(__name__)

ITEM_OUTPUT = ["itemid", "hostid", "name", "key_", "value_type", "delay", "units", "history", "trends", "status", "state"]
HISTORY_PAGE = 10000


class ZabbixAPIError(Exception):
    def __init__(self, method: str, code: int, message: str, data: str = "") -> None:
        super().__init__(f"{method}: {message} {data}".strip())
        self.method = method
        self.code = code
        self.data = data


class ZabbixAPI:
    def __init__(self, url: str, token: str | None = None, verify: bool = True, timeout: float = 60) -> None:
        url = url.rstrip("/")
        if not url.endswith("api_jsonrpc.php"):
            url += "/api_jsonrpc.php"
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify
        self._id = 0
        self._session_login = False

    # --- transport ---------------------------------------------------------
    def call(self, method: str, params: Any = None, auth: bool = True) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params if params is not None else {}, "id": self._id}
        headers = {"Content-Type": "application/json-rpc"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        log.debug("-> %s %s", method, json.dumps(params, ensure_ascii=False)[:2000])
        try:
            response = self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ZabbixAPIError(method, -1, f"HTTP request failed: {exc}") from None
        if response.status_code != 200:
            raise ZabbixAPIError(method, response.status_code, f"HTTP {response.status_code}", response.text[:300])
        try:
            body = response.json()
        except ValueError:
            raise ZabbixAPIError(method, -1, "non-JSON response", response.text[:300]) from None
        if "error" in body:
            error = body["error"]
            raise ZabbixAPIError(method, int(error.get("code", 0)), error.get("message", ""), error.get("data", ""))
        result = body.get("result")
        log.debug("<- %s %s", method, (json.dumps(result, ensure_ascii=False)[:500] if result is not None else None))
        return result

    def version(self) -> str:
        return str(self.call("apiinfo.version", auth=False))

    def login(self, username: str, password: str) -> None:
        self.token = str(self.call("user.login", {"username": username, "password": password}, auth=False))
        self._session_login = True

    def logout(self) -> None:
        if self._session_login and self.token:
            try:
                self.call("user.logout")
            except ZabbixAPIError as exc:  # pragma: no cover - best effort
                print(f"warning: logout failed: {exc}", file=sys.stderr)
            self.token = None
            self._session_login = False

    # --- entities ------------------------------------------------------------
    def get_trigger(self, triggerid: str) -> Trigger:
        rows = self.call(
            "trigger.get",
            {
                "triggerids": [triggerid],
                "output": "extend",
                "selectFunctions": "extend",
                "selectItems": ITEM_OUTPUT,
                "selectHosts": ["hostid", "host", "name"],
            },
        )
        if not rows:
            raise ZabbixAPIError("trigger.get", 0, f"trigger {triggerid} not found or not accessible")
        row = rows[0]
        expanded_rows = self.call(
            "trigger.get",
            {
                "triggerids": [triggerid],
                "output": ["triggerid", "expression", "recovery_expression", "description"],
                "expandExpression": True,
                "expandDescription": True,
            },
        )
        expanded = expanded_rows[0] if expanded_rows else {}
        hosts = {str(h["hostid"]): h["host"] for h in row.get("hosts", [])}
        items = {str(i["itemid"]): Item.from_api(i, hosts) for i in row.get("items", [])}
        functions = {
            str(f["functionid"]): TriggerFunction(str(f["functionid"]), str(f["itemid"]), f["name"], f.get("parameter", ""))
            for f in row.get("functions", [])
        }
        return Trigger(
            triggerid=str(row["triggerid"]),
            description=expanded.get("description", row.get("description", "")),
            expression=row.get("expression", ""),
            recovery_expression=row.get("recovery_expression", ""),
            expanded_expression=expanded.get("expression", ""),
            expanded_recovery_expression=expanded.get("recovery_expression", ""),
            recovery_mode=int(row.get("recovery_mode", 0)),
            type=int(row.get("type", 0)),
            priority=int(row.get("priority", 0)),
            status=int(row.get("status", 0)),
            state=int(row.get("state", 0)),
            value=int(row.get("value", 0)),
            manual_close=int(row.get("manual_close", 0)),
            items=items,
            hosts=hosts,
            functions=functions,
        )

    def find_item(self, host: str, key: str) -> Item | None:
        rows = self.call(
            "item.get",
            {"output": ITEM_OUTPUT, "host": host, "filter": {"key_": key}, "selectHosts": ["hostid", "host"], "limit": 1},
        )
        if not rows:
            return None
        row = rows[0]
        hosts = {str(h["hostid"]): h["host"] for h in row.get("hosts", [])}
        return Item.from_api(row, hosts)

    # --- data -------------------------------------------------------------
    def get_history(self, item: Item, time_from: int, time_till: int) -> list[Point]:
        points: dict[tuple[int, int], Point] = {}
        cursor = time_from
        while True:
            rows = self.call(
                "history.get",
                {
                    "output": "extend",
                    "history": item.value_type,
                    "itemids": [item.itemid],
                    "time_from": cursor,
                    "time_till": time_till,
                    "sortfield": "clock",
                    "sortorder": "ASC",
                    "limit": HISTORY_PAGE,
                },
            )
            new = 0
            for row in rows:
                point = point_from_api(item, row)
                if point.key not in points:
                    points[point.key] = point
                    new += 1
            if len(rows) < HISTORY_PAGE or new == 0:
                break
            last_clock = int(rows[-1]["clock"])
            if last_clock == cursor:
                # More than a page within one second - cannot paginate by clock.
                break
            cursor = last_clock
        return list(points.values())

    def get_history_before(self, item: Item, before_clock: int, count: int) -> list[Point]:
        if count <= 0:
            return []
        rows = self.call(
            "history.get",
            {
                "output": "extend",
                "history": item.value_type,
                "itemids": [item.itemid],
                "time_till": before_clock,
                "sortfield": "clock",
                "sortorder": "DESC",
                "limit": count,
            },
        )
        return [point_from_api(item, row) for row in rows]

    def get_trends(self, item: Item, time_from: int, time_till: int) -> list[TrendPoint]:
        rows = self.call(
            "trend.get",
            {
                "output": "extend",
                "itemids": [item.itemid],
                "time_from": time_from,
                "time_till": time_till,
            },
        )
        return [
            TrendPoint(int(r["clock"]), int(r["num"]), float(r["value_min"]), float(r["value_avg"]), float(r["value_max"]))
            for r in rows
        ]

    def get_trigger_events(self, triggerid: str, time_from: int, time_till: int) -> list[dict]:
        rows = self.call(
            "event.get",
            {
                "output": ["eventid", "clock", "ns", "value", "name"],
                "source": 0,
                "object": 0,
                "objectids": [triggerid],
                "time_from": time_from,
                "time_till": time_till,
                "sortfield": ["clock", "eventid"],
                "sortorder": "ASC",
            },
        )
        return [
            {"eventid": str(r["eventid"]), "clock": int(r["clock"]), "ns": int(r.get("ns", 0) or 0), "value": int(r["value"]), "name": r.get("name", "")}
            for r in rows
        ]

    def get_last_event_before(self, triggerid: str, before_clock: int) -> dict | None:
        rows = self.call(
            "event.get",
            {
                "output": ["eventid", "clock", "value"],
                "source": 0,
                "object": 0,
                "objectids": [triggerid],
                "time_till": before_clock - 1,
                "sortfield": ["clock", "eventid"],
                "sortorder": "DESC",
                "limit": 1,
            },
        )
        if not rows:
            return None
        return {"eventid": str(rows[0]["eventid"]), "clock": int(rows[0]["clock"]), "value": int(rows[0]["value"])}
