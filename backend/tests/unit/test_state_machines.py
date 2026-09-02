"""状态机单元测试（需求 6.1 / 6.2 / 架构 7.1 / 7.2）。"""

from __future__ import annotations

import pytest

from app.constants import (
    PLAN_ALLOWED_TRANSITIONS,
    PLAN_APPROVED,
    PLAN_DRAFT,
    PLAN_PENDING_APPROVAL,
    PLAN_REJECTED,
    PLAN_SUPERSEDED,
    PO_ALLOWED_TRANSITIONS,
    PO_CANCELLED,
    PO_CLOSED,
    PO_CREATED,
    PO_ORDER_UNKNOWN,
    PO_ORDERED,
    PO_ORDERING,
    PO_PARTIALLY_RECEIVED,
    PO_RECEIVED,
)

PLAN_STATES = [PLAN_DRAFT, PLAN_PENDING_APPROVAL, PLAN_APPROVED, PLAN_REJECTED, PLAN_SUPERSEDED]
PO_STATES = [
    PO_CREATED,
    PO_ORDERING,
    PO_ORDERED,
    PO_ORDER_UNKNOWN,
    PO_PARTIALLY_RECEIVED,
    PO_RECEIVED,
    PO_CLOSED,
    PO_CANCELLED,
]


@pytest.mark.parametrize("state", PLAN_STATES)
def test_plan_states_have_transition_entries(state):
    assert state in PLAN_ALLOWED_TRANSITIONS


@pytest.mark.parametrize("state", PO_STATES)
def test_po_states_have_transition_entries(state):
    assert state in PO_ALLOWED_TRANSITIONS


def test_plan_terminal_states():
    assert PLAN_ALLOWED_TRANSITIONS[PLAN_REJECTED] == ()
    assert PLAN_ALLOWED_TRANSITIONS[PLAN_SUPERSEDED] == ()


def test_po_terminal_states():
    assert PO_ALLOWED_TRANSITIONS[PO_CLOSED] == ()
    assert PO_ALLOWED_TRANSITIONS[PO_CANCELLED] == ()


def test_po_cancelled_only_from_created():
    assert PO_ALLOWED_TRANSITIONS[PO_ORDERED] == (PO_PARTIALLY_RECEIVED, PO_RECEIVED)
    assert PO_CANCELLED not in PO_ALLOWED_TRANSITIONS[PO_ORDERED]
    assert PO_CANCELLED not in PO_ALLOWED_TRANSITIONS[PO_ORDER_UNKNOWN]
    assert PO_CANCELLED not in PO_ALLOWED_TRANSITIONS[PO_PARTIALLY_RECEIVED]
    assert PO_CANCELLED in PO_ALLOWED_TRANSITIONS[PO_CREATED]


def test_order_unknown_recovery():
    assert PO_ORDERED in PO_ALLOWED_TRANSITIONS[PO_ORDER_UNKNOWN]
    assert PO_CREATED in PO_ALLOWED_TRANSITIONS[PO_ORDER_UNKNOWN]


def test_no_direct_jumps():
    """采购状态图简写不能解释为任意跳转。"""
    assert PO_ORDERED not in PO_ALLOWED_TRANSITIONS[PO_CREATED]  # 建单不等于下单
    assert PO_CLOSED in PO_ALLOWED_TRANSITIONS[PO_RECEIVED]  # 收齐后由 buyer 显式确认关闭
    assert PO_ORDERING not in PO_ALLOWED_TRANSITIONS[PO_RECEIVED]  # 已收齐不可再下单
    assert PO_CANCELLED not in PO_ALLOWED_TRANSITIONS[PO_ORDER_UNKNOWN]  # 未知状态只能查询恢复
    assert PO_CREATED in PO_ALLOWED_TRANSITIONS[PO_ORDERING]  # 明确失败回到 po_created
    assert PO_ORDERED in PO_ALLOWED_TRANSITIONS[PO_ORDERING]  # 明确成功进入 ordered
    assert PO_ORDER_UNKNOWN in PO_ALLOWED_TRANSITIONS[PO_ORDERING]  # 歧义进入未知状态


def test_plan_approved_superseded_only_before_po():
    assert PLAN_ALLOWED_TRANSITIONS[PLAN_APPROVED] == (PLAN_SUPERSEDED,)
