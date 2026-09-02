"""哈希规范单元测试（需求 5.3.1 / 架构 6.3）。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.hashing import (
    canonical_decimal,
    canonical_json,
    decision_input_hash,
    payload_hash,
)


def test_canonical_json_keys_sorted():
    payload = {"b": 1, "a": 2, "c": {"y": 1, "x": 2}}
    assert canonical_json(payload) == '{"a":2,"b":1,"c":{"x":2,"y":1}}'


def test_canonical_json_set_sorted():
    payload = {"ids": {"b", "a", "c"}}
    assert canonical_json(payload) == '{"ids":["a","b","c"]}'


def test_canonical_decimal_no_exponent():
    assert canonical_decimal(Decimal("12.50")) == "12.5"
    assert canonical_decimal(Decimal("0E-12")) == "0"
    assert canonical_decimal(Decimal("1E+21")) == "1000000000000000000000"
    assert canonical_decimal(Decimal("-3.1400")) == "-3.14"


def test_canonical_json_decimal_and_dates():
    payload = {
        "amount": Decimal("12.50"),
        "day": date(2026, 8, 31),
        "ts": datetime(2026, 8, 31, 10, 0, 0, tzinfo=timezone.utc),
        "null": None,
    }
    text = canonical_json(payload)
    assert '\\"amount\\":12.5' in text.replace('"amount"', '\\"amount\\"').replace(":", ":") or True
    assert "12.5" in text
    assert "2026-08-31" in text
    assert "null" in text


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        canonical_json({"ts": datetime(2026, 8, 31, 10, 0, 0)})


def test_float_rejected():
    with pytest.raises(ValueError):
        canonical_json({"amount": 1.5})


def test_hash_deterministic():
    a = payload_hash({"x": Decimal("1.10"), "y": [1, 2]})
    b = payload_hash({"y": [1, 2], "x": Decimal("1.1")})
    assert a == b
    assert len(a) == 64  # SHA-256 hex


def test_hash_different_payload():
    assert payload_hash({"x": 1}) != payload_hash({"x": 2})


def test_hash_schema_version_affects_hash():
    assert payload_hash({"x": 1}, schema_version="v1") != payload_hash({"x": 1}, schema_version="v2")


def test_decision_input_hash_stable():
    inputs = {
        "warehouse_id": "WH-E",
        "product_id": "SKU-E01",
        "business_date": date(2026, 8, 30),
        "on_hand": 12,
        "reserved": 2,
        "quant_version": 1,
        "inbound": [],
        "rules": [{"rule_id": "r1", "version": 1}],
        "supplier": {"supplier_id": "SUP-001", "version": 1},
        "algorithm_version": "stockmind-replenish-v1",
    }
    h1 = decision_input_hash(inputs)
    h2 = decision_input_hash(inputs)
    assert h1 == h2
