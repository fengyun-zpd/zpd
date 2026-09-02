"""全局异常处理：稳定错误码 + request/trace id（需求 8.2）。

数据库唯一约束冲突只返回稳定错误码，不暴露数据库异常细节（需求 6.1.1）。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import OperationInProgressError, StockMindError

logger = logging.getLogger("stockmind.api")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _error_body(request: Request, code: str, message: str, detail: object = None) -> dict:
    body: dict[str, object] = {
        "request_id": _request_id(request),
        "code": code,
        "message": message,
    }
    if detail is not None:
        body["detail"] = detail
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StockMindError)
    async def _stockmind_error_handler(request: Request, exc: StockMindError) -> JSONResponse:
        body = _error_body(request, exc.code, exc.message, exc.detail)
        if isinstance(exc, OperationInProgressError):
            body["operation_id"] = (exc.detail or {}).get("operation_id")
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "HTTP_ERROR"
        if exc.status_code == 404:
            code = "NOT_FOUND"
        elif exc.status_code == 422:
            code = "VALIDATION_ERROR"
        elif exc.status_code == 403:
            code = "FORBIDDEN"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(request, code, str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body(request, "VALIDATION_ERROR", "请求参数校验失败", exc.errors()),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("integrity error: %s", exc.orig)
        return JSONResponse(
            status_code=409,
            content=_error_body(request, "CONFLICT", "数据唯一性约束冲突（不暴露数据库细节）"),
        )

    @app.exception_handler(OperationalError)
    async def _operational_handler(request: Request, exc: OperationalError) -> JSONResponse:
        logger.warning("operational error: %s", exc.orig)
        if "canceling statement due to lock timeout" in str(exc.orig):
            return JSONResponse(
                status_code=409,
                content=_error_body(request, "RESOURCE_BUSY", "获取事务锁超时，请以同一幂等键重试"),
            )
        return JSONResponse(
            status_code=500,
            content=_error_body(request, "INTERNAL_ERROR", "数据库操作失败"),
        )

    @app.exception_handler(Exception)
    async def _generic_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(request, "INTERNAL_ERROR", "服务器内部错误"),
        )
