"""持久化 workflow_resume 触发任务（需求 3.2 / 架构 3 / ADR 决策 7）。

审批/驳回/替代事务提交后写入 workflow_resume，由 Celery 异步触发 LangGraph resume；
恢复键为 thread_id + plan_id + decision_version。失败/重复/checkpoint 丢失不回滚业务决定。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.models.agent import WorkflowResume
from app.tasks.celery_app import celery

logger = logging.getLogger("stockmind.tasks.resume")


@celery.task(name="stockmind.trigger_workflow_resume", bind=True)
def trigger_workflow_resume(self) -> dict:
    from app.db import get_session_factory

    factory = get_session_factory()
    results = {"triggered": 0, "failed": 0}
    with factory() as session:
        pending = session.scalars(
            select(WorkflowResume)
            .where(WorkflowResume.status == "pending")
            .order_by(WorkflowResume.created_at)
            .limit(20)
        ).all()
        for resume in pending:
            try:
                from app.agent.graph import resume_workflow

                resume_workflow(
                    thread_id=resume.thread_id,
                    plan_id=resume.plan_id,
                    decision_version=resume.decision_version,
                )
                resume.status = "succeeded"
                session.commit()
                results["triggered"] += 1
            except Exception as exc:  # noqa: BLE001 恢复失败只记录告警，不回滚业务决定
                resume.attempts += 1
                resume.last_error = f"{type(exc).__name__}: {exc}"
                session.commit()
                results["failed"] += 1
                logger.warning(
                    "workflow resume failed thread=%s plan=%s v=%s: %s",
                    resume.thread_id,
                    resume.plan_id,
                    resume.decision_version,
                    exc,
                )
    return results
