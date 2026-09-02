"""领域异常体系（稳定错误码）。

规范性定义：需求 5.3 / 6.1 / 6.4、架构 7.1 / 7.3。
"""

from __future__ import annotations

from app.constants import (
    ERR_ACTIVE_REPLENISHMENT_EXISTS,
    ERR_BLOCKED,
    ERR_CONFLICT,
    ERR_DATA_UNAVAILABLE,
    ERR_EXTERNAL_UNKNOWN,
    ERR_FORBIDDEN,
    ERR_IDEMPOTENCY_KEY_REUSED,
    ERR_INTERNAL,
    ERR_INVALID_STATE_TRANSITION,
    ERR_LLM_UNAVAILABLE,
    ERR_NOT_FOUND,
    ERR_OPERATION_IN_PROGRESS,
    ERR_PLAN_STALE,
    ERR_RECEIPT_EVENT_REUSED,
    ERR_RECEIPT_EXCEEDS_REMAINING,
    ERR_RESOURCE_BUSY,
    ERR_VALIDATION,
    ERR_VERSION_CONFLICT,
)


class StockMindError(Exception):
    """领域错误基类。"""

    code: str = ERR_INTERNAL
    http_status: int = 500
    detail: dict | None = None

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ForbiddenError(StockMindError):
    code = ERR_FORBIDDEN
    http_status = 403


class NotFoundError(StockMindError):
    code = ERR_NOT_FOUND
    http_status = 404


class ValidationError(StockMindError):
    code = ERR_VALIDATION
    http_status = 422


class VersionConflictError(StockMindError):
    code = ERR_VERSION_CONFLICT
    http_status = 409


class InvalidStateTransitionError(StockMindError):
    code = ERR_INVALID_STATE_TRANSITION
    http_status = 409


class PlanStaleError(StockMindError):
    code = ERR_PLAN_STALE
    http_status = 409


class ActiveReplenishmentExistsError(StockMindError):
    code = ERR_ACTIVE_REPLENISHMENT_EXISTS
    http_status = 409


class IdempotencyKeyReusedError(StockMindError):
    code = ERR_IDEMPOTENCY_KEY_REUSED
    http_status = 409


class OperationInProgressError(StockMindError):
    code = ERR_OPERATION_IN_PROGRESS
    http_status = 202


class ResourceBusyError(StockMindError):
    code = ERR_RESOURCE_BUSY
    http_status = 409


class ReceiptEventReusedError(StockMindError):
    code = ERR_RECEIPT_EVENT_REUSED
    http_status = 409


class ReceiptExceedsRemainingError(StockMindError):
    code = ERR_RECEIPT_EXCEEDS_REMAINING
    http_status = 409


class BlockedInputError(StockMindError):
    code = ERR_BLOCKED
    http_status = 409


class DataUnavailableError(StockMindError):
    code = ERR_DATA_UNAVAILABLE
    http_status = 409


class ConflictError(StockMindError):
    code = ERR_CONFLICT
    http_status = 409


class ExternalUnknownError(StockMindError):
    code = ERR_EXTERNAL_UNKNOWN
    http_status = 502


class LLMUnavailableError(StockMindError):
    code = ERR_LLM_UNAVAILABLE
    http_status = 503


class InternalError(StockMindError):
    code = ERR_INTERNAL
    http_status = 500
