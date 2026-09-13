"""对话 API：SSE 流式对话（需求 8.2 / 架构 3）。

SSE 事件（真流式 + 断线续传）：agent_start / node_end / tool_call / draft_created /
interrupted / message / done / error。每个事件带 ``id: turn_id:seq`` 序号行，
客户端断线后携 ``Last-Event-ID`` 续传；续传只重放未收到的进度，不重新执行 Agent。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import RequestContext, get_context
from app.db import get_session_factory
from app.errors import ForbiddenError, NotFoundError, ValidationError
from app.models.agent import Conversation, Message
from app.services.access import require_roles
from app.streaming import classify_last_event_id, sse_replay_frames, stream_agent_turn

router = APIRouter(prefix="/api/v1", tags=["conversations"])


class CreateConversationRequest(BaseModel):
    """兼容旧客户端；身份以 X-Actor-Id 为唯一事实源。"""

    actor_id: str | None = None


class SendMessageRequest(BaseModel):
    content: str


def _owned_conversation(session, thread_id: str, actor_id: str) -> Conversation:
    conv = session.query(Conversation).filter(Conversation.thread_id == thread_id).first()
    if conv is None:
        raise NotFoundError(f"会话不存在 {thread_id}")
    if conv.actor_id != actor_id:
        raise ForbiddenError("无权访问其他演示用户的会话")
    return conv


@router.post("/conversations")
def create_conversation(
    ctx: Annotated[RequestContext, Depends(get_context)],
    body: CreateConversationRequest | None = None,
) -> dict:
    """创建当前请求身份的会话；请求体 actor_id 仅为兼容字段，不具备授权作用。"""
    factory = get_session_factory()
    thread_id = uuid.uuid4().hex
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        if body and body.actor_id and body.actor_id != ctx.actor_id:
            raise ForbiddenError("请求体 actor_id 必须与 X-Actor-Id 一致")
        session.add(Conversation(thread_id=thread_id, actor_id=ctx.actor_id))
        session.commit()
        return {
            "request_id": ctx.request_id,
            "data": {"thread_id": thread_id, "actor_id": ctx.actor_id},
        }


@router.get("/conversations/{thread_id}/messages")
def list_messages(thread_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        conv = _owned_conversation(session, thread_id, ctx.actor_id)
        messages = session.query(Message).filter(Message.conversation_id == conv.id).order_by(Message.created_at).all()
        return {
            "request_id": ctx.request_id,
            "data": [{"role": m.role, "content": m.content} for m in messages],
        }


@router.post("/conversations/{thread_id}/messages")
async def send_message(
    thread_id: str,
    body: SendMessageRequest,
    ctx: Annotated[RequestContext, Depends(get_context)],
    request: Request,
) -> StreamingResponse:
    """SSE 流式执行一轮 Agent 对话；支持 ``Last-Event-ID`` 断线续传。

    正常路径：逐节点流式产出事件并写入缓冲；续传路径：重放缓冲中未收到的事件，
    缓冲缺失时回退 checkpoint 读取最终态重放最小事件集。续传不重新执行 Agent，
    避免重复 generate_draft 触发防重副作用（流式层幂等）。
    """
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        _owned_conversation(session, thread_id, ctx.actor_id)  # 归属校验（含续传）

    # 断线续传：客户端带 Last-Event-ID 重连，只重放已产生的事件（不重新执行 Agent）。
    # 非法游标必须显式拒绝（稳定 422），不能静默当作新请求而重新执行一轮 Agent。
    last_event_status, parsed = classify_last_event_id(request.headers.get("last-event-id"))
    if last_event_status == "invalid":
        raise ValidationError("Last-Event-ID 格式非法（应为 turn_id:seq，seq 为非负整数）")
    if parsed is not None:
        replay_turn_id, after_seq = parsed
        return StreamingResponse(
            sse_replay_frames(thread_id=thread_id, turn_id=replay_turn_id, after_seq=after_seq),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 正常路径：记录用户消息后流式执行
    turn_id = uuid.uuid4().hex
    with factory() as session:
        conv = _owned_conversation(session, thread_id, ctx.actor_id)
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
        headers={
            "X-Request-Id": ctx.request_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关代理缓冲，保证实时推送
        },
    )
