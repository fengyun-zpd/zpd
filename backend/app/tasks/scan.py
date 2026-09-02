"""定时扫描任务（需求 2.2 / 架构 8）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from croniter import croniter
from sqlalchemy import select

from app.models.governance import Execution, Schedule
from app.services.execution_service import run_scan
from app.tasks.celery_app import celery

logger = logging.getLogger("stockmind.tasks.scan")


@celery.task(name="stockmind.dispatch_due_schedules", bind=True)
def dispatch_due_schedules(self) -> int:
    """Beat 调度分发：读取数据库 Schedule 配置，为到期的周期生成扫描任务。"""
    from app.db import get_session_factory

    dispatched = 0
    factory = get_session_factory()
    with factory() as session:
        schedules = session.scalars(select(Schedule).where(Schedule.enabled.is_(True))).all()
        now = datetime.now(timezone.utc)
        for schedule in schedules:
            # 该计划最近一次执行（按计划时间）
            last = session.scalar(
                select(Execution)
                .where(Execution.schedule_id == schedule.id)
                .order_by(Execution.scheduled_at.desc())
                .limit(1)
            )
            try:
                itr = croniter(schedule.cron_expr, now, ret_type=datetime)
                next_due = itr.get_next(datetime)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cron 解析失败 schedule=%s: %s", schedule.id, exc)
                continue
            last_due = last.scheduled_at if last else None
            if last_due is None or next_due > last_due:
                # 已到期且未执行：提交异步扫描（DB 唯一约束防重）
                scan_schedule.delay(
                    schedule_id=schedule.id,
                    scheduled_at=next_due.isoformat(),
                    idempotency_key=f"scan:{schedule.id}:{next_due.isoformat()}",
                )
                dispatched += 1
    return dispatched


@celery.task(name="stockmind.scan_schedule", bind=True, acks_late=True, max_retries=2)
def scan_schedule(self, schedule_id: str, scheduled_at: str, idempotency_key: str) -> str:
    """执行一次计划扫描（同一 schedule_id+scheduled_at 幂等防重）。"""
    from app.db import get_session_factory

    parsed = datetime.fromisoformat(scheduled_at)
    factory = get_session_factory()
    with factory() as session:
        try:
            execution_id = run_scan(
                session,
                schedule_id=schedule_id,
                scheduled_at=parsed,
                trigger_type="scheduled",
                triggered_by_actor_id=None,
                idempotency_key=idempotency_key,
            )
            session.commit()
            return execution_id
        except Exception:
            session.rollback()
            raise
