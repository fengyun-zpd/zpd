"""统一幂等操作记录（需求 6.4 / 架构 7.3 / ADR 决策 8）。

作用域 = (principal_id, command_type, aggregate_ref, idempotency_key)。
- 同键同载荷：成功/终止失败后返回保存结果；retryable 前置失败可在同一记录上恢复；
- 同键异载荷：HTTP 409 IDEMPOTENCY_KEY_REUSED；
- 进行中：HTTP 202 OPERATION_IN_PROGRESS（租约过期可接管）；
- 外部结果不确定（unknown）：不得重放，必须走供应商查询恢复。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import (
    OP_FAILED,
    OP_IN_PROGRESS,
    OP_RETRYABLE,
    OP_SUCCEEDED,
    OP_UNKNOWN,
)
from app.errors import (
    ConflictError,
    ExternalUnknownError,
    IdempotencyKeyReusedError,
    OperationInProgressError,
    StockMindError,
)
from app.models.governance import IdempotencyOperation
from app.services.error_map import _ERROR_BY_CODE
from app.services.hashing import payload_hash

LEASE_TTL = timedelta(minutes=10)


@dataclass
class BeginResult:
    action: str  # proceed / replay_success / replay_failure / in_progress / unknown
    response: Any = None
    error_code: str | None = None
    error_message: str | None = None
    operation: IdempotencyOperation | None = None


class IdempotencyGuard:
    """命令级幂等守卫；与业务命令共用同一个数据库事务。"""

    def __init__(
        self,
        session: Session,
        *,
        principal_id: str,
        command_type: str,
        aggregate_ref: str,
        idempotency_key: str,
        payload: Any,
    ) -> None:
        self.session = session
        self.principal_id = principal_id
        self.command_type = command_type
        self.aggregate_ref = aggregate_ref
        self.idempotency_key = idempotency_key
        self.payload_hash = payload_hash(payload)
        self.operation: IdempotencyOperation | None = None

    def _find(self) -> IdempotencyOperation | None:
        stmt = select(IdempotencyOperation).where(
            IdempotencyOperation.principal_id == self.principal_id,
            IdempotencyOperation.command_type == self.command_type,
            IdempotencyOperation.aggregate_ref == self.aggregate_ref,
            IdempotencyOperation.idempotency_key == self.idempotency_key,
        )
        return self.session.scalar(stmt)

    def begin(self) -> BeginResult:
        existing = self._find()
        now = datetime.now(timezone.utc)
        if existing is None:
            op = IdempotencyOperation(
                principal_id=self.principal_id,
                command_type=self.command_type,
                aggregate_ref=self.aggregate_ref,
                idempotency_key=self.idempotency_key,
                payload_hash=self.payload_hash,
                hash_schema_version="stockmind-hash-v1",
                status=OP_IN_PROGRESS,
                lease_until=now + LEASE_TTL,
            )
            self.session.add(op)
            try:
                self.session.flush()
            except IntegrityError:
                # 并发创建同一作用域+键：回滚到保存点后读取既有记录
                self.session.rollback()
                existing = self._find()
                if existing is None:  # pragma: no cover
                    raise
            else:
                self.operation = op
                return BeginResult(action="proceed", operation=op)

        if existing.payload_hash != self.payload_hash:
            raise IdempotencyKeyReusedError(
                "相同幂等键但载荷不同，拒绝执行",
                detail={"idempotency_key": self.idempotency_key},
            )

        if existing.status == OP_SUCCEEDED:
            return BeginResult(
                action="replay_success",
                response=existing.response,
                operation=existing,
            )
        if existing.status == OP_FAILED:
            code = (existing.response or {}).get("error_code") or "INTERNAL_ERROR"
            message = (existing.response or {}).get("error_message") or "已记录的命令失败"
            return BeginResult(
                action="replay_failure",
                error_code=code,
                error_message=message,
                operation=existing,
            )
        if existing.status == OP_UNKNOWN:
            raise ExternalUnknownError(
                "该命令已产生外部不确定结果，禁止重放；请先查询供应商状态恢复",
                detail={"operation_id": existing.id},
            )
        if existing.status in (OP_IN_PROGRESS, OP_RETRYABLE):
            lease = existing.lease_until
            if lease is not None and lease > now and existing.status == OP_IN_PROGRESS:
                raise OperationInProgressError(
                    "命令执行中，请稍后查询操作状态",
                    detail={"operation_id": existing.id},
                )
            # 租约过期或 retryable：允许接管同一操作记录
            existing.status = OP_IN_PROGRESS
            existing.lease_until = now + LEASE_TTL
            self.operation = existing
            return BeginResult(action="proceed", operation=existing)

        raise ConflictError(f"未知的幂等操作状态 {existing.status}")  # pragma: no cover

    def succeed(self, response: Any, *, entity_type: str | None = None, entity_id: str | None = None) -> None:
        op = self.operation
        if op is None:  # pragma: no cover
            return
        op.status = OP_SUCCEEDED
        op.response = response
        op.lease_until = None
        if entity_type:
            op.entity_type = entity_type
        if entity_id:
            op.entity_id = entity_id

    def fail(self, error: StockMindError) -> None:
        op = self.operation
        if op is None:
            return
        op.status = OP_FAILED
        op.response = {
            "error_code": error.code,
            "error_message": error.message,
            "detail": error.detail,
        }
        op.lease_until = None

    def mark_retryable(self) -> None:
        """前置瞬时失败（副作用发生前）标记为可恢复。"""
        op = self.operation
        if op is None:
            return
        op.status = OP_RETRYABLE
        op.lease_until = datetime.now(timezone.utc) + LEASE_TTL

    def mark_unknown(self) -> None:
        """外部副作用结果不确定：不得重放，只能走查询恢复。"""
        op = self.operation
        if op is None:
            return
        op.status = OP_UNKNOWN
        op.lease_until = None


def replay_failure(result: BeginResult) -> None:
    """把已记录的终止失败按稳定错误码重新抛出。"""
    error_cls = _ERROR_BY_CODE.get(result.error_code or "", StockMindError)
    raise error_cls(result.error_message or "已记录的命令失败")
