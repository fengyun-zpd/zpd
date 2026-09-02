"""V1 收口功能优化测试：错误可见性与恢复提示（不改变领域语义）。

- blocked_hints：阻断码 -> 明确下一步动作；
- node_draft：防重/阻断输出结构化 outcome + blocked_lines；
- node_clarify：缺参时明确列出需补充字段。
"""

from __future__ import annotations

from app.agent.blocked_hints import blocked_line_summary, next_step


def test_next_step_mapping_covers_known_codes():
    """关键阻断码都有明确下一步动作，且不含 Agent 自主副作用指令。"""
    # Agent 无权执行的副作用动词：下一步动作中不得引导 Agent 直接执行
    forbidden_verbs = ["直接审批", "代为下单", "自动收货", "自动关闭", "自动取消", "修改规则", "切换故障"]
    for code in [
        "no_rule",
        "rule_conflict",
        "illegal_input",
        "no_supplier",
        "data_insufficient",
        "ACTIVE_REPLENISHMENT_EXISTS",
        "PLAN_STALE",
        "IDEMPOTENCY_KEY_REUSED",
        "EXTERNAL_UNKNOWN",
        "RESOURCE_BUSY",
    ]:
        hint = next_step(code)
        assert hint is not None, f"{code} 缺少下一步提示"
        title, action = hint
        assert title and action
        for verb in forbidden_verbs:
            assert verb not in action, f"{code} 下一步动作含 Agent 禁止副作用: {verb}"


def test_blocked_line_summary_structure():
    """阻断行摘要包含 product_id / code / reason / next_step。"""
    s = blocked_line_summary("SKU-E01", "no_supplier", "无候选供应商关系")
    assert s["product_id"] == "SKU-E01"
    assert s["blocked_code"] == "no_supplier"
    assert s["blocked_reason"] == "无候选供应商关系"
    assert "供应商" in s["next_step"]


def test_blocked_line_unknown_code_fallback():
    """未知阻断码回退到兜底文案，不抛错。"""
    s = blocked_line_summary("SKU-X", "some_future_code", "未知原因")
    assert s["blocked_code"] == "some_future_code"
    assert s["next_step"]  # 兜底文案存在


def test_active_suggestion_next_step_points_to_approval():
    """活动建议已存在的下一步必须是'去审批箱处理'，而非重复发起。"""
    _, action = next_step("ACTIVE_REPLENISHMENT_EXISTS")
    assert "审批箱" in action


def test_external_unknown_forbids_blind_retry():
    """order_unknown 相关提示必须明确'只查询恢复、禁止盲目重试'。"""
    _, action = next_step("EXTERNAL_UNKNOWN")
    assert "查询" in action
    assert "重试" in action or "重下" in action


def test_plan_stale_next_step_offers_recompute_or_exclude():
    """PLAN_STALE 下一步提供排除或重算入口。"""
    _, action = next_step("PLAN_STALE")
    assert "排除" in action or "修订" in action or "变化" in action
