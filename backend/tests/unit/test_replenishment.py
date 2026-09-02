"""补货量计算单元测试（需求 5.3 / 架构 6.3）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.errors import BlockedInputError
from app.services.replenishment import ReplenishmentInput, compute_replenishment


def _input(**kw) -> ReplenishmentInput:
    base = dict(
        requested_window=14,
        daily_forecast=Decimal("10"),
        fixed_safety_stock=Decimal("30"),
        lead_days=5,
        review_period_days=3,
        minimum_order_qty=0,
        pack_multiple=1,
        on_hand=50,
        reserved=5,
        inbound=Decimal("0"),
    )
    base.update(kw)
    return ReplenishmentInput(**base)


def test_basic_formula():
    r = compute_replenishment(_input())
    assert r.available == Decimal("45")
    assert r.planning_window == 14  # max(14, 5+3)
    assert r.coverage_demand == Decimal("140")
    assert r.target_stock == Decimal("170")
    assert r.net_demand == Decimal("125")
    assert r.order_qty == 125


def test_window_uses_lead_plus_review():
    r = compute_replenishment(_input(lead_days=20, review_period_days=10))
    assert r.planning_window == 30


def test_zero_net_demand_zero_qty():
    r = compute_replenishment(_input(on_hand=500))
    assert r.net_demand == Decimal("0")
    assert r.order_qty == 0


def test_pack_multiple_ceiling():
    r = compute_replenishment(_input(on_hand=0, reserved=0, inbound=Decimal("0"), pack_multiple=25))
    # net = 170 -> ceil(170/25)=7 -> 175
    assert r.order_qty == 175


def test_minimum_order_qty():
    r = compute_replenishment(_input(on_hand=160, minimum_order_qty=50, pack_multiple=25))
    # net = 10 -> max(10,50)=50 -> ceil(50/25)=2 -> 50
    assert r.order_qty == 50


def test_inbound_deduction():
    r = compute_replenishment(_input(inbound=Decimal("100")))
    assert r.net_demand == Decimal("25")
    assert r.order_qty == 25


def test_fallback_forecast_coverage_zero():
    r = compute_replenishment(_input(daily_forecast=None))
    assert r.fallback is True
    assert r.coverage_demand == Decimal("0")
    assert r.target_stock == Decimal("30")


def test_reserved_gt_on_hand_blocked():
    with pytest.raises(BlockedInputError):
        compute_replenishment(_input(on_hand=10, reserved=20))


def test_pack_multiple_zero_blocked():
    with pytest.raises(BlockedInputError):
        compute_replenishment(_input(pack_multiple=0))


def test_negative_values_blocked():
    with pytest.raises(BlockedInputError):
        compute_replenishment(_input(on_hand=-1))
    with pytest.raises(BlockedInputError):
        compute_replenishment(_input(lead_days=-1))
    with pytest.raises(BlockedInputError):
        compute_replenishment(_input(minimum_order_qty=-5))


def test_invalid_window_blocked():
    with pytest.raises(BlockedInputError):
        compute_replenishment(_input(requested_window=21))


def test_over_business_cap_blocked():
    with pytest.raises(BlockedInputError):
        compute_replenishment(_input(on_hand=10**15))


def test_decimal_12_scale():
    r = compute_replenishment(_input())
    assert str(r.target_stock).split(".")[1] == "000000000000"
    assert str(r.net_demand).split(".")[1] == "000000000000"


def test_round_half_up_on_12_scale():
    """ROUND_HALF_UP：中间值不提前取整，持久化 12 位。"""
    r = compute_replenishment(_input(daily_forecast=Decimal("10.12345678901234567890")))
    # coverage = 10.12345678901234567890 * 14 = 141.7283950461728395046 -> 12位 ROUND_HALF_UP
    assert str(r.coverage_demand) == "141.728395046173"
