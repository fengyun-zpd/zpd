"""Agent 死循环与最大步数防护测试（需求 3 扩展 / 宪法第十六条）。

覆盖：最大步数触发、相同工具相同参数触发、相同工具不同参数不触发、
触发后不调用写工具、SSE 返回明确的人工转交结果。
"""

from __future__ import annotations

import pytest

from app.agent.graph import ESCALATE_RESPONSE, astream_turn, node_draft, node_gather_evidence, run_turn
from app.config import get_settings

_FULL_INPUT = "帮我检查华东仓未来两周需要补货的紧固件"
_PARAMS_A = {"warehouse_id": "WH-E", "products": ["SKU-E01"], "requested_window": 14}
_PARAMS_B = {"warehouse_id": "WH-E", "products": ["SKU-E02"], "requested_window": 14}


def _set_env(monkeypatch, **env: str) -> None:
    """设置环境变量并清掉 settings 缓存（config 使用 lru_cache）。"""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


async def _collect(thread_id: str, text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async for event, data in astream_turn(thread_id, "alice", text):
        events.append((event, data))
    return events


@pytest.mark.db
def test_max_steps_escalates(db_session, monkeypatch):
    """步数超过 AGENT_MAX_STEPS 时进入 escalate（转人工），且不再生成草稿。"""
    _set_env(monkeypatch, AGENT_MAX_STEPS="1")
    try:
        state, interrupted = run_turn("t-guard-steps", "alice", _FULL_INPUT)
        assert interrupted is False
        assert state.get("outcome") == "escalated"
        assert state.get("loop_blocked") is True
        assert state.get("response") == ESCALATE_RESPONSE
        assert state.get("plan_id") is None  # 未进入 draft，无业务副作用
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_same_tool_same_args_escalates(db_session, monkeypatch):
    """同一会话中相同工具 + 相同参数超过上限：loop_blocked 且转人工。"""
    _set_env(monkeypatch, AGENT_TOOL_DUPLICATE_LIMIT="1")
    try:
        first = node_gather_evidence({"params": _PARAMS_A, "tool_calls": [], "tool_call_counts": {}})
        assert not first.get("loop_blocked")

        counts = first.get("tool_call_counts", {})
        assert counts, "工具调用计数应被记录"

        second = node_gather_evidence(
            {"params": _PARAMS_A, "tool_calls": [], "tool_call_counts": dict(counts)}
        )
        assert second.get("loop_blocked") is True
        assert second.get("outcome") == "escalated"
        assert second.get("response") == ESCALATE_RESPONSE
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_same_tool_different_args_not_escalated(db_session, monkeypatch):
    """相同工具但参数不同不得被误判为重复调用。"""
    _set_env(monkeypatch, AGENT_TOOL_DUPLICATE_LIMIT="1")
    try:
        first = node_gather_evidence({"params": _PARAMS_A, "tool_calls": [], "tool_call_counts": {}})
        counts = first.get("tool_call_counts", {})
        second = node_gather_evidence(
            {"params": _PARAMS_B, "tool_calls": [], "tool_call_counts": dict(counts)}
        )
        assert not second.get("loop_blocked"), "不同参数不应触发重复门禁"
        assert second.get("outcome") != "escalated"
        assert second.get("rule_evidence") is not None
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_guard_never_calls_write_tool(db_session, monkeypatch):
    """转人工路径不得调用受控写工具 generate_draft。"""
    called = {"count": 0}
    from app.agent import tools as agent_tools

    original = agent_tools.generate_draft

    def _spy(**kwargs):
        called["count"] += 1
        return original(**kwargs)

    monkeypatch.setattr(agent_tools, "generate_draft", _spy)
    _set_env(monkeypatch, AGENT_MAX_STEPS="1")
    try:
        state, _ = run_turn("t-guard-write", "alice", _FULL_INPUT)
        assert state.get("outcome") == "escalated"
        assert called["count"] == 0, "转人工后不得调用写工具"
    finally:
        get_settings.cache_clear()


@pytest.mark.db
async def test_astream_escalate_reports_handoff(db_session, monkeypatch):
    """SSE 路径应给出明确的人工转交结果（message + done 均标记 loop_blocked）。"""
    _set_env(monkeypatch, AGENT_MAX_STEPS="1")
    try:
        events = await _collect("t-guard-stream", _FULL_INPUT)
        names = [event for event, _ in events]
        assert "message" in names and "done" in names

        message = next(data for event, data in events if event == "message")
        assert message["outcome"] == "escalated"
        assert message["loop_blocked"] is True
        assert "转人工" in message["content"]
        assert message.get("step_count")

        done = next(data for event, data in events if event == "done")
        assert done["loop_blocked"] is True
        assert done.get("step_count")
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_step_count_persisted_in_state(db_session, monkeypatch):
    """步数来自状态（可被 checkpoint 保存），正常路径也应有稳定计数。"""
    _set_env(monkeypatch, AGENT_MAX_STEPS="50")
    try:
        state, _ = run_turn("t-guard-count", "alice", "帮我补货")
        assert isinstance(state.get("step_count"), int)
        assert state.get("step_count") >= 1
        assert state.get("outcome") == "clarified"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------- 写工具纳入同一门禁


def _draft_state(params: dict, counts: dict | None = None, thread_id: str = "t-draft") -> dict:
    return {
        "actor_id": "alice",
        "thread_id": thread_id,
        "params": params,
        "tool_calls": [],
        "tool_call_counts": dict(counts or {}),
    }


@pytest.mark.db
def test_duplicate_generate_draft_blocked_before_domain_call(db_session, monkeypatch):
    """相同 generate_draft 参数超过限制：不执行领域服务、不残留草稿、转人工。"""
    from app.agent import tools as agent_tools

    _set_env(monkeypatch, AGENT_TOOL_DUPLICATE_LIMIT="1")
    called = {"count": 0}
    original = agent_tools.generate_draft

    def _spy(**kwargs):
        called["count"] += 1
        return original(**kwargs)

    monkeypatch.setattr(agent_tools, "generate_draft", _spy)
    try:
        first = node_draft(_draft_state(_PARAMS_A, thread_id="t-dup-draft"))
        assert called["count"] == 1, "首次应真正调用领域服务"
        counts = first.get("tool_call_counts", {})
        assert counts, "写工具调用也必须记入计数（可随 checkpoint 保存）"

        second = node_draft(_draft_state(_PARAMS_A, counts=counts, thread_id="t-dup-draft"))
        assert called["count"] == 1, "超限后不得再次调用领域服务"
        assert second.get("loop_blocked") is True
        assert second.get("outcome") == "escalated"
        assert second.get("response") == ESCALATE_RESPONSE
        # 不得残留看似成功的草稿 / 计划结果
        assert second.get("draft_result") is None
        assert second.get("plan_id") is None
        assert second.get("needs_approval") is False
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_generate_draft_different_params_not_duplicate(db_session, monkeypatch):
    """不同参数的 generate_draft 不得被误判为重复调用。"""
    _set_env(monkeypatch, AGENT_TOOL_DUPLICATE_LIMIT="1")
    try:
        first = node_draft(_draft_state(_PARAMS_A, thread_id="t-draft-a"))
        assert first.get("outcome") == "draft_created"
        counts = first.get("tool_call_counts", {})

        second = node_draft(_draft_state(_PARAMS_B, counts=counts, thread_id="t-draft-b"))
        assert second.get("loop_blocked") is not True, "不同参数不得触发重复门禁"
        assert second.get("outcome") != "escalated"
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_generate_draft_product_order_normalized(db_session, monkeypatch):
    """products 顺序不同但集合相同，视为同一请求（key 会规范化排序）。"""
    _set_env(monkeypatch, AGENT_TOOL_DUPLICATE_LIMIT="1")
    params_multi = {"warehouse_id": "WH-E", "products": ["SKU-E03", "SKU-E01"], "requested_window": 14}
    params_reordered = {"warehouse_id": "WH-E", "products": ["SKU-E01", "SKU-E03"], "requested_window": 14}
    try:
        first = node_draft(_draft_state(params_multi, thread_id="t-order-1"))
        counts = first.get("tool_call_counts", {})
        second = node_draft(_draft_state(params_reordered, counts=counts, thread_id="t-order-2"))
        assert second.get("loop_blocked") is True, "同集合不同顺序必须视为同一请求"
        assert second.get("outcome") == "escalated"
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_blocked_draft_creates_no_new_plan(db_session, monkeypatch):
    """门禁拦截后不产生新的业务副作用（计划数量不变）。"""
    from sqlalchemy import func, select

    from app.models.replenishment import ReplenishmentPlan

    _set_env(monkeypatch, AGENT_TOOL_DUPLICATE_LIMIT="1")
    try:
        first = node_draft(_draft_state(_PARAMS_A, thread_id="t-dup-se"))
        counts = first.get("tool_call_counts", {})
        db_session.expire_all()
        before = db_session.scalar(select(func.count()).select_from(ReplenishmentPlan))

        second = node_draft(_draft_state(_PARAMS_A, counts=counts, thread_id="t-dup-se"))
        assert second.get("outcome") == "escalated"

        db_session.expire_all()
        after = db_session.scalar(select(func.count()).select_from(ReplenishmentPlan))
        assert after == before, "被门禁拦截的调用不得新建补货计划"
    finally:
        get_settings.cache_clear()
