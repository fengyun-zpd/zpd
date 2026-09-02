"""Celery 应用（需求 2.2 / 架构 8）。"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery = Celery(
    "stockmind",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.scan",
        "app.tasks.recovery",
        "app.tasks.resume_trigger",
    ],
)

celery.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_default_queue="stockmind",
    task_routes={
        "stockmind.dispatch_due_schedules": {"queue": "stockmind"},
        "stockmind.scan_schedule": {"queue": "stockmind"},
        "stockmind.recover_unknown_orders": {"queue": "stockmind"},
        "stockmind.trigger_workflow_resume": {"queue": "stockmind"},
    },
    beat_schedule={
        # Beat 每 30 秒触发调度分发任务；真正周期由数据库 Schedule 配置决定
        "dispatch-due-schedules": {
            "task": "stockmind.dispatch_due_schedules",
            "schedule": 30.0,
        },
        "recover-unknown-orders": {
            "task": "stockmind.recover_unknown_orders",
            "schedule": crontab(minute="*/1"),
        },
        "trigger-workflow-resume": {
            "task": "stockmind.trigger_workflow_resume",
            "schedule": crontab(minute="*/1"),
        },
    },
)
