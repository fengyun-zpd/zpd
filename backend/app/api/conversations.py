"""对话 API：SSE 流式对话（需求 8.2 / 架构 3）。

SSE 事件：agent_start / tool_call / draft_created / interrupted / message / done / error。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import RequestContext, get_context
from app.db import get_session_factory
from app.errors import StockMindError
from app.models.agent import Conversation, Message
from app.services.access import require_roles

logger = logging.getLogger("stockmind.api.conversations")

router = APIRouter(prefix="/api/v1", tags=["conversations"])


class CreateConversationRequest(BaseModel):
    actor_id: str


class SendMessageRequest(BaseModel):
    content: str


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/conversations")
def create_conversation(body: CreateConversationRequest) -> dict:
    factory = get_session_factory()
    thread_id = uuid.uuid4().hex
    with factory() as session:
        require_roles(session, body.actor_id, "operator", "approver", "buyer", "admin")
        session.add(Conversation(thread_id=thread_id, actor_id=body.actor_id))
        session.commit()
        return {
            "request_id": uuid.uuid4().hex,
            "data": {"thread_id": thread_id, "actor_id": body.actor_id},
        }


@router.get("/conversations/{thread_id}/messages")
def list_messages(thread_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        conv = session.query(Conversation).filter(Conversation.thread_id == thread_id).first()
        if conv is None:
            from app.errors import NotFoundError

            raise NotFoundError(f"会话不存在 {thread_id}")
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
) -> StreamingResponse:
    """SSE 流式执行一轮 Agent 对话。"""
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        conv = session.query(Conversation).filter(Conversation.thread_id == thread_id).first()
        if conv is None:
            from app.errors import NotFoundError

            raise NotFoundError(f"会话不存在 {thread_id}")
        session.add(Message(conversation_id=conv.id, role="user", content=body.content))
        session.commit()

    async def event_stream():
        from app.observability import mark_error, set_request_id

        # 通过 contextvar 把 request_id 关联到本轮观测 trace（run_turn 内部创建 trace）
        set_request_id(ctx.request_id)
        yield _sse("agent_start", {"thread_id": thread_id})
        try:
            from app.agent.graph import run_turn

            # 同步图执行在事件循环外运行，避免阻塞（V1 离线模式耗时极短）
            state, interrupted = await asyncio.to_thread(run_turn, thread_id, ctx.actor_id, body.content)
            for call in state.get("tool_calls", []):
                yield _sse("tool_call", call)
            if state.get("draft_result") and state["draft_result"].get("created"):
                yield _sse("draft_created", {"plan_id": state["draft_result"]["plan_id"]})
            if interrupted:
                yield _sse("interrupted", {"status": "pending_approval"})
            yield _sse(
                "message",
                {
                    "content": state.get("response", ""),
                    "offline": state.get("offline", False),
                    "outcome": state.get("outcome"),
                    "blocked_lines": state.get("blocked_lines"),
                    "missing_params": state.get("missing_params"),
                },
            )
            yield _sse("done", {})
        except StockMindError as exc:
            mark_error(f"{exc.code}: {exc.message}")
            yield _sse("error", {"code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001
            mark_error(f"{type(exc).__name__}: {exc}")
            logger.exception("agent turn failed")
            yield _sse("error", {"code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Request-Id": ctx.request_id, "Cache-Control": "no-cache"},
    )
