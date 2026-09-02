"""LangGraph Agent 图运行测试（需求 3 / 架构 3 / ADR 决策 7）。

覆盖：离线模式一轮补货（澄清/草稿+interrupt）、resume 读取数据库决定、
工具调用审计、必要澄清。checkpoint 只恢复会话，业务状态以数据库为准。
"""

from __future__ import annotations

import pytest

from app.agent.graph import resume_workflow, run_turn
from app.agent.tools import TOOL_WHITELIST


@pytest.mark.db
def test_agent_offline_full_turn_creates_draft_and_interrupts(db_session):
    """补货意图 + 完整参数 -> 证据工具 -> 受控草稿 -> interrupt。"""
    state, interrupted = run_turn("t1", "alice", "帮我检查华东仓未来两周需要补货的紧固件")
    assert interrupted is True
    assert state.get("intent") == "replenish"
    assert state.get("offline") is True
    assert state.get("plan_id")
    # 只读工具 + 草稿工具，无副作用工具
    tools_used = {c["tool"] for c in state.get("tool_calls", [])}
    assert "generate_draft" in tools_used
    assert tools_used <= TOOL_WHITELIST

    # 数据库确实创建了待审批计划
    from sqlalchemy import select

    from app.models.replenishment import PlanLine, ReplenishmentPlan

    with db_session_factory_ctx() as session:
        plan = session.scalar(select(ReplenishmentPlan).where(ReplenishmentPlan.id == state["plan_id"]))
        assert plan is not None
        assert plan.status == "pending_approval"
        lines = session.scalars(select(PlanLine).where(PlanLine.plan_id == plan.id)).all()
        assert lines and all(line.active_for_dedupe for line in lines)


@pytest.mark.db
def test_agent_clarifies_when_params_missing(db_session):
    state, interrupted = run_turn("t2", "alice", "帮我补货")
    assert interrupted is False
    assert state.get("missing_params")
    response = state.get("response", "")
    # 澄清使用自然业务语言：不暴露内部字段名，明确引导仓库/SKU/规划周期
    for internal in ("warehouse_id", "products", "requested_window", "budget"):
        assert internal not in response
    assert "仓库" in response
    assert "SKU" in response
    assert "7/14/30" in response or "7 天 / 14 天 / 30 天" in response


@pytest.mark.db
def test_agent_resume_reads_db_decision(db_session):
    """审批（REST 落库）后 resume：只读取业务决定并解释，不执行审批。"""
    state, interrupted = run_turn("t3", "alice", "帮我检查华东仓未来两周需要补货的紧固件")
    assert interrupted is True
    plan_id = state["plan_id"]

    # 模拟审批人通过 REST 落库（领域服务路径）
    from sqlalchemy import select

    from app.models.replenishment import PlanLine
    from app.services import plan_service

    with db_session_factory_ctx() as session:
        line = session.scalar(select(PlanLine).where(PlanLine.plan_id == plan_id))
        plan_service.decide_plan(
            session,
            plan_id=plan_id,
            actor_id="carol",
            mode="approve",
            decisions={line.id: "approve"},
            idempotency_key="agent-resume-app-1",
        )
        session.commit()

    resumed = resume_workflow("t3", plan_id, decision_version=2)
    assert "审批通过" in resumed.get("response", "")


def db_session_factory_ctx():
    """返回一个新会话（context manager 用法）。"""
    from app.db import get_session_factory

    return get_session_factory()()
