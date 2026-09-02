"""错误码 -> 异常类映射（供幂等重放与 API 层复用）。"""

from __future__ import annotations

from app import errors

_ERROR_BY_CODE: dict[str, type[errors.StockMindError]] = {
    errors.ERR_FORBIDDEN: errors.ForbiddenError,
    errors.ERR_NOT_FOUND: errors.NotFoundError,
    errors.ERR_VALIDATION: errors.ValidationError,
    errors.ERR_VERSION_CONFLICT: errors.VersionConflictError,
    errors.ERR_INVALID_STATE_TRANSITION: errors.InvalidStateTransitionError,
    errors.ERR_PLAN_STALE: errors.PlanStaleError,
    errors.ERR_ACTIVE_REPLENISHMENT_EXISTS: errors.ActiveReplenishmentExistsError,
    errors.ERR_IDEMPOTENCY_KEY_REUSED: errors.IdempotencyKeyReusedError,
    errors.ERR_OPERATION_IN_PROGRESS: errors.OperationInProgressError,
    errors.ERR_RESOURCE_BUSY: errors.ResourceBusyError,
    errors.ERR_RECEIPT_EVENT_REUSED: errors.ReceiptEventReusedError,
    errors.ERR_RECEIPT_EXCEEDS_REMAINING: errors.ReceiptExceedsRemainingError,
    errors.ERR_BLOCKED: errors.BlockedInputError,
    errors.ERR_DATA_UNAVAILABLE: errors.DataUnavailableError,
    errors.ERR_EXTERNAL_UNKNOWN: errors.ExternalUnknownError,
    errors.ERR_CONFLICT: errors.ConflictError,
    errors.ERR_INTERNAL: errors.InternalError,
}
