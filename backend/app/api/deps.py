"""API 公共依赖：actor 解析、幂等键、请求上下文。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel

from app.services.access import ensure_not_browser_system


class RequestContext(BaseModel):
    request_id: str
    actor_id: str
    idempotency_key: str | None = None
    trace_id: str | None = None


def make_request_id() -> str:
    return uuid.uuid4().hex


def get_context(
    request: Request,
    x_actor_id: Annotated[str | None, Header()] = None,
    x_idempotency_key: Annotated[str | None, Header()] = None,
) -> RequestContext:
    """构造请求上下文；REST 请求禁止以 system 冒充（宪法第九条）。"""
    request_id = getattr(request.state, "request_id", make_request_id())
    if not x_actor_id:
        raise HTTPException(status_code=422, detail="缺少 X-Actor-Id 请求头")
    ensure_not_browser_system(x_actor_id)
    return RequestContext(
        request_id=request_id,
        actor_id=x_actor_id,
        idempotency_key=x_idempotency_key,
    )


def require_idempotency_key(ctx: RequestContext) -> str:
    if not ctx.idempotency_key:
        raise HTTPException(status_code=422, detail="状态变更请求必须携带 X-Idempotency-Key")
    return ctx.idempotency_key
