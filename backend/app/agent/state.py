"""LangGraph 补货助手 Agent（需求 3 / 架构 3 / ADR 决策 1、7）。"""

from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    thread_id: str
    actor_id: str
    user_input: str
    intent: str  # replenish / query / explain / other
    params: dict  # warehouse_id / products / requested_window
    missing_params: list[str]
    clarification: str | None
    tool_calls: list[dict]
    rule_evidence: list[dict]
    draft_result: dict | None
    plan_id: str | None
    needs_approval: bool
    decision: dict | None  # resume 后：{status, plan_id, decision_version}
    response: str
    offline: bool
    # 功能优化（V1 收口）：结构化结果，供前端明确展示阻断原因与下一步动作
    outcome: str  # draft_created / blocked / duplicate / clarified / answered
    blocked_lines: list[dict]  # 每行：{product_id, blocked_code, blocked_reason, next_step}
