"""HTTP Agent 主链路（V1.1 扩展能力 / 需求 8.2 / 架构 10）。

把 Agent 从"仅对话页驱动"升级为**可编程的 HTTP 主链路**：

- ``POST /api/v1/agent/start``：自然语言启动一轮，创建会话并沿用 SSE 事件流；
- ``POST /api/v1/agent/{thread_id}/resume``：同一会话继续（澄清补充）或读取已提交
  审批决定后恢复（``decision_version`` 版本校验）；
- ``GET  /api/v1/agent/{thread_id}/state``：读取当前 Agent 状态的安全摘要。

边界（宪法第七、八、九、十二条）：

- ``resume`` **不执行审批、不下单、不产生任何业务副作用**，只读取数据库已提交的
  决定并恢复对话流程；
- 业务状态始终以业务数据库为准，LangGraph checkpoint 只用于流程恢复；
- 身份事实源是 ``X-Actor-Id``，并校验会话归属（禁止跨演示用户访问）；
- 状态摘要不返回密钥、完整 Prompt 或不必要的敏感检索内容。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.conversations import _owned_conversation
from app.api.deps import RequestContext, get_context
from app.db import get_session_factory
from app.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.models.agent import Conversation, Message
from app.models.replenishment import ReplenishmentPlan
from app.services.access import require_roles
from app.streaming import classify_last_event_id, sse_replay_frames, stream_agent_resume, stream_agent_turn

router = APIRouter(prefix="/api/v1", tags=["agent"])

_ALLOWED_ROLES = ("operator", "approver", "buyer", "admin")

_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class AgentStartRequest(BaseModel):
    """启动一轮 Agent 对话（自然语言）。"""

    content: str


class AgentResumeRequest(BaseModel):
    """恢复请求：澄清补充（``content``）或审批恢复（``plan_id`` + ``decision_version``）。"""

    content: str | None = None
    plan_id: str | None = None
    decision_version: int | None = None


def _stream_headers(ctx: RequestContext, thread_id: str) -> dict:
    return {"X-Request-Id": ctx.request_id, "X-Thread-Id": thread_id, **_STREAM_HEADERS}


# 已产生可恢复决定的状态：审批通过 / 驳回 / 被修订替代
_DECIDED_PLAN_STATUSES = ("approved", "rejected", "superseded")


def _checkpoint_awaiting_resume(thread_id: str, plan_id: str) -> bool:
    """checkpoint 是否确实处于等待恢复：存在待执行节点且其 ``plan_id`` 与请求一致。

    只用于判定"这是有效恢复点"，不执行任何业务动作；checkpoint 不是业务事实源
    （宪法第十二条），业务决定仍以数据库为准。
    """
    from app.agent.graph import get_graph

    try:
        snapshot = get_graph().get_state({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 checkpoint 不可用时按"非等待恢复"处理
        return False
    if snapshot is None:
        return False
    pending = tuple(snapshot.next or ())
    if not pending:
        return False  # 无待执行节点 = 流程未挂起（已结束或从未中断）
    values = snapshot.values or {}
    return values.get("plan_id") == plan_id


@router.post("/agent/start")
async def agent_start(
    body: AgentStartRequest,
    ctx: Annotated[RequestContext, Depends(get_context)],
    request: Request,
) -> StreamingResponse:
    """自然语言启动一轮 Agent：创建会话、持久化用户消息、SSE 流式执行。

    ``X-Actor-Id`` 是身份唯一事实源；响应头包含 ``X-Request-Id`` 与 ``X-Thread-Id``。
    """
    last_event_status, _parsed = classify_last_event_id(request.headers.get("last-event-id"))
    if last_event_status == "invalid":
        raise ValidationError("Last-Event-ID 格式非法（应为 turn_id:seq，seq 为非负整数）")
    if last_event_status == "valid":
        # start 会在本次请求内创建新 thread，无法定位上一轮轮次；续传请用 resume 入口
        raise ValidationError(
            "agent/start 不支持断线续传（本轮 thread 由本次请求创建）；"
            "请使用 POST /api/v1/agent/{thread_id}/resume 携带 Last-Event-ID"
        )

    factory = get_session_factory()
    thread_id = uuid.uuid4().hex
    turn_id = uuid.uuid4().hex
    with factory() as session:
        require_roles(session, ctx.actor_id, *_ALLOWED_ROLES)
        conv = Conversation(thread_id=thread_id, actor_id=ctx.actor_id)
        session.add(conv)
        session.flush()  # 取得 conv.id（default 在 flush 时生成）
        session.add(Message(conversation_id=conv.id, role="user", content=body.content))
        session.commit()

    return StreamingResponse(
        stream_agent_turn(
            thread_id=thread_id,
            turn_id=turn_id,
            content=body.content,
            actor_id=ctx.actor_id,
            request_id=ctx.request_id,
        ),
        media_type="text/event-stream",
        headers=_stream_headers(ctx, thread_id),
    )


@router.post("/agent/{thread_id}/resume")
async def agent_resume(
    thread_id: str,
    body: AgentResumeRequest,
    ctx: Annotated[RequestContext, Depends(get_context)],
    request: Request,
) -> StreamingResponse:
    """恢复同一会话：澄清补充（``content``）或读取已提交审批决定（``plan_id`` + 版本）。

    审批恢复只读数据库已提交决定并解释结果，**不执行审批、建单、下单或任何副作用**。
    恢复键严格为 ``thread_id + plan_id + decision_version``，并要求：会话属于当前 actor、
    计划存在、计划绑定到本会话、决定版本一致、计划已有可恢复决定、checkpoint 确实处于
    等待恢复；违反者分别返回稳定 403 / 404 / 409。
    """
    factory = get_session_factory()

    # 断线续传优先：只重放已产生的事件，不重新执行 Agent（避免重复副作用）。
    # 非法游标显式拒绝（稳定 422），不得静默开启新一轮任务。
    last_event_status, parsed = classify_last_event_id(request.headers.get("last-event-id"))
    if last_event_status == "invalid":
        raise ValidationError("Last-Event-ID 格式非法（应为 turn_id:seq，seq 为非负整数）")
    if parsed is not None:
        replay_turn_id, after_seq = parsed
        with factory() as session:
            require_roles(session, ctx.actor_id, *_ALLOWED_ROLES)
            _owned_conversation(session, thread_id, ctx.actor_id)
        return StreamingResponse(
            sse_replay_frames(thread_id=thread_id, turn_id=replay_turn_id, after_seq=after_seq),
            media_type="text/event-stream",
            headers=_stream_headers(ctx, thread_id),
        )

    has_content = bool(body.content and body.content.strip())
    has_decision = body.plan_id is not None or body.decision_version is not None
    if has_content and has_decision:
        raise ValidationError("resume 只能二选一：澄清补充（content）或审批恢复（plan_id + decision_version）")
    if not has_content and not has_decision:
        raise ValidationError("resume 需要提供 content（澄清补充）或 plan_id + decision_version（审批恢复）")

    turn_id = uuid.uuid4().hex

    if has_content:
        # 澄清补充：同一 thread 继续（不新建会话，不产生业务副作用）
        with factory() as session:
            require_roles(session, ctx.actor_id, *_ALLOWED_ROLES)
            conv = _owned_conversation(session, thread_id, ctx.actor_id)
            session.add(Message(conversation_id=conv.id, role="user", content=body.content or ""))
            session.commit()
        return StreamingResponse(
            stream_agent_turn(
                thread_id=thread_id,
                turn_id=turn_id,
                content=body.content or "",
                actor_id=ctx.actor_id,
                request_id=ctx.request_id,
            ),
            media_type="text/event-stream",
            headers=_stream_headers(ctx, thread_id),
        )

    if body.plan_id is None or body.decision_version is None:
        raise ValidationError("审批恢复必须同时提供 plan_id 与 decision_version")

    # 审批恢复前置校验（只读、无副作用）：会话归属 -> 计划存在 -> 计划属于本会话
    # -> 决定版本一致 -> 计划已产生可恢复决定 -> checkpoint 确实处于等待恢复。
    with factory() as session:
        require_roles(session, ctx.actor_id, *_ALLOWED_ROLES)
        _owned_conversation(session, thread_id, ctx.actor_id)
        plan = session.get(ReplenishmentPlan, body.plan_id)
        if plan is None:
            raise NotFoundError(f"补货计划不存在 {body.plan_id}")
        # 计划必须绑定到当前会话：禁止用其他会话的 plan_id 触发恢复
        if plan.thread_id != thread_id:
            raise ForbiddenError(
                f"计划 {body.plan_id} 不属于当前会话，禁止跨会话恢复",
                detail={"requested_thread_id": thread_id, "plan_thread_id": plan.thread_id},
            )
        current_version = int(plan.version)
        if current_version != int(body.decision_version):
            raise VersionConflictError(
                f"决定版本不匹配：当前 {current_version}，请求 {body.decision_version}",
                detail={"current_version": current_version, "requested_version": int(body.decision_version)},
            )
        if plan.status not in _DECIDED_PLAN_STATUSES:
            raise ConflictError(
                f"计划 {body.plan_id} 当前状态为 {plan.status}，尚无可恢复的已提交决定",
                detail={"status": plan.status, "expected": list(_DECIDED_PLAN_STATUSES)},
            )

    # 恢复键严格为 thread_id + plan_id + decision_version；并要求 checkpoint 处于等待恢复
    if not _checkpoint_awaiting_resume(thread_id, body.plan_id):
        raise ConflictError(
            "会话当前不在等待恢复状态（checkpoint 无待恢复节点或 plan_id 不一致）",
            detail={"thread_id": thread_id, "plan_id": body.plan_id},
        )

    return StreamingResponse(
        stream_agent_resume(
            thread_id=thread_id,
            turn_id=turn_id,
            plan_id=body.plan_id,
            decision_version=int(body.decision_version),
            request_id=ctx.request_id,
        ),
        media_type="text/event-stream",
        headers=_stream_headers(ctx, thread_id),
    )


@router.get("/agent/{thread_id}/state")
def agent_state(
    thread_id: str,
    ctx: Annotated[RequestContext, Depends(get_context)],
) -> dict:
    """读取当前 Agent 状态的安全摘要（归属校验后从 checkpoint 读取）。"""
    from app.agent.graph import get_graph

    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, *_ALLOWED_ROLES)
        _owned_conversation(session, thread_id, ctx.actor_id)

    snapshot = get_graph().get_state({"configurable": {"thread_id": thread_id}})
    state = (snapshot.values if snapshot else None) or {}
    pending = tuple(snapshot.next) if snapshot is not None and snapshot.next else ()
    return {
        "request_id": ctx.request_id,
        "data": {
            "thread_id": thread_id,
            "intent": state.get("intent"),
            "missing_params": list(state.get("missing_params") or []),
            "offline": state.get("offline"),
            "degradation_reason": state.get("degradation_reason"),
            "step_count": state.get("step_count"),
            "outcome": state.get("outcome"),
            "plan_id": state.get("plan_id"),
            "needs_approval": bool(state.get("needs_approval")),
            "loop_blocked": bool(state.get("loop_blocked")),
            "response": state.get("response"),
            # 附加只读诊断字段（不含密钥 / 完整 Prompt / 检索正文）
            "pending_nodes": list(pending),
        },
    }
