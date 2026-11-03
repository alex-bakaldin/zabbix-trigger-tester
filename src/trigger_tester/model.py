"""Plain data objects for Zabbix entities used by the tester."""

from __future__ import annotations

from dataclasses import dataclass, field

VALUE_TYPE_NAMES = {0: "float", 1: "char", 2: "log", 3: "uint", 4: "text", 5: "binary"}
NUMERIC_VALUE_TYPES = {0, 3}
PRIORITY_NAMES = {
    0: "Not classified",
    1: "Information",
    2: "Warning",
    3: "Average",
    4: "High",
    5: "Disaster",
}
RECOVERY_MODE_NAMES = {0: "expression", 1: "recovery expression", 2: "none"}


@dataclass
class Item:
    itemid: str
    hostid: str
    host: str
    name: str
    key_: str
    value_type: int
    delay: str = ""
    units: str = ""
    history: str = ""
    trends: str = ""
    status: int = 0
    state: int = 0

    @property
    def is_numeric(self) -> bool:
        return self.value_type in NUMERIC_VALUE_TYPES

    @property
    def value_type_name(self) -> str:
        return VALUE_TYPE_NAMES.get(self.value_type, str(self.value_type))

    @property
    def ref(self) -> str:
        return f"/{self.host}/{self.key_}"

    @classmethod
    def from_api(cls, data: dict, hosts: dict[str, str]) -> "Item":
        return cls(
            itemid=str(data["itemid"]),
            hostid=str(data["hostid"]),
            host=hosts.get(str(data["hostid"]), str(data.get("host", data["hostid"]))),
            name=data.get("name", ""),
            key_=data["key_"],
            value_type=int(data["value_type"]),
            delay=data.get("delay", ""),
            units=data.get("units", ""),
            history=data.get("history", ""),
            trends=data.get("trends", ""),
            status=int(data.get("status", 0)),
            state=int(data.get("state", 0)),
        )


@dataclass
class TriggerFunction:
    functionid: str
    itemid: str
    name: str
    parameter: str


@dataclass
class Trigger:
    triggerid: str
    description: str
    expression: str
    recovery_expression: str
    expanded_expression: str
    expanded_recovery_expression: str
    recovery_mode: int
    type: int
    priority: int
    status: int
    state: int = 0
    value: int = 0
    manual_close: int = 0
    items: dict[str, Item] = field(default_factory=dict)
    hosts: dict[str, str] = field(default_factory=dict)
    functions: dict[str, TriggerFunction] = field(default_factory=dict)

    @property
    def priority_name(self) -> str:
        return PRIORITY_NAMES.get(self.priority, str(self.priority))

    @property
    def recovery_mode_name(self) -> str:
        return RECOVERY_MODE_NAMES.get(self.recovery_mode, str(self.recovery_mode))

    def item_by_ref(self, host: str, key: str) -> Item | None:
        for item in self.items.values():
            if item.host == host and item.key_ == key:
                return item
        return None
