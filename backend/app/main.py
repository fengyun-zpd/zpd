"""FastAPI 应用工厂（需求 8.2 / 架构 10）。"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request

from app.api import governance, knowledge, plans, purchase
from app.api.errors import register_exception_handlers
from app.config import get_settings

logger = logging.getLogger("stockmind")


def create_app() -> FastAPI:
    settings = get_settings()
    agent_available = False
    app = FastAPI(
        title="StockMind API",
        version="0.1.0",
        description="可审计的智能仓储补货 Agent（V1 本机闭环）",
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Duration-Ms"] = str(duration_ms)
        return response

    register_exception_handlers(app)

    app.include_router(plans.router)
    app.include_router(purchase.router)
    app.include_router(governance.router)
    app.include_router(knowledge.router)

    # 对话（LangGraph）路由在 Agent 模块可用后挂载；先注册健康检查
    @app.get("/health")
    def health() -> dict:
        # liveness 只回答进程是否存活；能力状态由 capabilities 暴露，避免
        # Agent 依赖缺失时把“进程活着”误报成“业务完整可用”。
        return {
            "status": "ok",
            "env": settings.env,
            "capabilities": {"agent": agent_available},
        }

    @app.get("/ready")
    def ready() -> dict:
        if not agent_available:
            raise HTTPException(
                status_code=503,
                detail={"status": "degraded", "checks": {"agent": False}},
            )
        return {"status": "ready", "env": settings.env, "checks": {"agent": True}}

    @app.get("/")
    def root() -> dict:
        return {
            "name": "StockMind API",
            "version": "0.1.0",
            "docs": "/docs",
        }

    # 延迟挂载对话与 Agent 主链路路由，避免 Agent 依赖不可用时阻断应用启动
    try:
        from app.api.agent import router as agent_router
        from app.api.conversations import router as conversations_router

        app.include_router(conversations_router)
        app.include_router(agent_router)
        agent_available = True
    except ImportError:  # pragma: no cover
        logger.warning("conversations/agent router 未加载：Agent 模块尚不可用")

    return app


app = create_app()
