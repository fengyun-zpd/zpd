"""供应商选择单元测试（需求 5.2 / 架构 6.2）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.supplier import SupplierCandidate, select_supplier

BUSINESS_DATE = date(2026, 6, 1)


def _cand(
    supplier_id,
    *,
    priority=100,
    lead=5,
    price="1.0",
    min_qty=0,
    pack=1,
    enabled=True,
    frm=None,
    to=None,
) -> SupplierCandidate:
    return SupplierCandidate(
        supplier_id=supplier_id,
        business_priority=priority,
        lead_days=lead,
        price=Decimal(price),
        minimum_order_qty=min_qty,
        pack_multiple=pack,
        enabled=enabled,
        effective_from=frm,
        effective_to=to,
    )


def test_select_by_priority():
    chosen, _, reason = select_supplier([_cand("SUP-B", priority=50), _cand("SUP-A", priority=10)], BUSINESS_DATE)
    assert chosen.supplier_id == "SUP-A"


def test_tie_break_by_lead_time():
    chosen, _, _ = select_supplier(
        [_cand("SUP-A", priority=10, lead=5), _cand("SUP-B", priority=10, lead=2)],
        BUSINESS_DATE,
    )
    assert chosen.supplier_id == "SUP-B"


def test_tie_break_by_price():
    chosen, _, _ = select_supplier(
        [
            _cand("SUP-A", priority=10, lead=2, price="1.5"),
            _cand("SUP-B", priority=10, lead=2, price="1.2"),
        ],
        BUSINESS_DATE,
    )
    assert chosen.supplier_id == "SUP-B"


def test_tie_break_by_supplier_id():
    chosen, _, _ = select_supplier(
        [
            _cand("SUP-B", priority=10, lead=2, price="1.2"),
            _cand("SUP-A", priority=10, lead=2, price="1.2"),
        ],
        BUSINESS_DATE,
    )
    assert chosen.supplier_id == "SUP-A"  # 字典序升序


def test_disabled_filtered():
    chosen, filtered, _ = select_supplier([_cand("SUP-A", enabled=False), _cand("SUP-B", enabled=True)], BUSINESS_DATE)
    assert chosen.supplier_id == "SUP-B"
    assert all(c.enabled for c in filtered)


def test_out_of_period_filtered():
    chosen, _, _ = select_supplier(
        [
            _cand("SUP-A", frm=date(2026, 7, 1)),
            _cand("SUP-B", to=date(2026, 1, 1)),
            _cand("SUP-C"),
        ],
        BUSINESS_DATE,
    )
    assert chosen.supplier_id == "SUP-C"


def test_illegal_values_filtered():
    chosen, _, _ = select_supplier(
        [
            SupplierCandidate(supplier_id="SUP-A", lead_days=-1, price=Decimal("1")),
            SupplierCandidate(supplier_id="SUP-B", pack_multiple=0, price=Decimal("1")),
            SupplierCandidate(supplier_id="SUP-C", price=None),
            SupplierCandidate(supplier_id="SUP-D", price=Decimal("1"), lead_days=3, pack_multiple=2),
        ],
        BUSINESS_DATE,
    )
    assert chosen.supplier_id == "SUP-D"


def test_no_candidates():
    chosen, filtered, reason = select_supplier([], BUSINESS_DATE)
    assert chosen is None
    assert reason == "无候选供应商关系"


def test_all_filtered():
    chosen, _, reason = select_supplier([_cand("SUP-A", enabled=False)], BUSINESS_DATE)
    assert chosen is None
    assert "过滤" in reason
