from __future__ import annotations

import pytest

from trigger_tester.history import Point, Series
from trigger_tester.model import Item


def make_item(itemid="1", host="srv", key="cpu", value_type=0, **kw) -> Item:
    return Item(itemid=itemid, hostid="10", host=host, name=key, key_=key, value_type=value_type, delay="1m", **kw)


def make_series(item: Item, values, start=1_700_000_000, interval=60) -> Series:
    points = [Point(start + i * interval, 0, v) for i, v in enumerate(values)]
    return Series(item, points)


@pytest.fixture
def float_item():
    return make_item()


@pytest.fixture
def text_item():
    return make_item(itemid="2", key="status", value_type=1)
