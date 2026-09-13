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
    budget_note: bool  # 用户输入含预算/成本约束：V1 不参与计算，仅在回复中说明
    degradation_reason: str | None  # LLM 未配置或失败时的可审计降级原因
    step_count: int  # LangGraph 节点执行步数（含当前节点）
    tool_call_counts: dict[str, int]  # 工具+规范化参数计数，防止同会话重复调用死循环
    loop_blocked: bool  # 超步数或重复工具调用后转人工
