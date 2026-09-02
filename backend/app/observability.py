"""Langfuse 可选观测（需求 8.2 / 架构 11 / ADR 决策 6）。

设计约束（宪法第十五、十七条）：
- 未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 时完全不初始化客户端（干净 no-op，
  不打印 SDK 日志、不产生任何网络请求），项目照常运行；
- 配置凭证后记录：对话 trace、LLM generation、工具调用 span、RAG 检索 span、
  领域计算 span 与错误，关联 request_id / trace id / thread id / plan id；
- 记录模型名、耗时、Token 使用量与调用状态；
- 对 API Key、原始敏感文本执行脱敏；业务标识（plan_id 等）只出现在 metadata 中用于关联；
- 观测失败（网络/异常）绝不改变业务事务结果：所有调用都 try/except 吞掉并记 warning。

采用 langfuse 2.x 低层 API（trace/span/generation），不依赖 langchain callback，
避免版本耦合；langfuse 依赖在 rag extra（Docker 镜像安装，CI 的 [dev] 不装）。
"""

from __future__ import annotations

import contextvars
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import get_settings

logger = logging.getLogger("stockmind.observability")

# 模块级单例客户端（延迟初始化）
_client: Any = None

# 当前 turn 的 trace 上下文（contextvars：每个并发 turn 相互隔离）
_current_trace: contextvars.ContextVar[Any] = contextvars.ContextVar("langfuse_trace", default=None)

# 当前请求的 request_id（由 API 层设置，关联到 trace metadata）
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("langfuse_request_id", default=None)

# Token 计数（独立于 Langfuse 客户端：无凭证时也累计，供本地评测采样）
_token_input: int = 0
_token_output: int = 0
_token_total: int = 0


def token_counts() -> tuple[int, int, int]:
    """返回 (input, output, total) 累计 Token（本地评测采样用）。"""
    return _token_input, _token_output, _token_total


def reset_token_counts() -> None:
    global _token_input, _token_output, _token_total
    _token_input = 0
    _token_output = 0
    _token_total = 0


def set_request_id(request_id: str | None) -> None:
    """设置当前请求的 request_id（API 层调用；与 turn_trace 在同一 context）。"""
    if request_id:
        _request_id.set(request_id)


# 常见敏感值模式（用于脱敏；不匹配业务标识）
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(api[_-]?key|secret|password|token)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{6,}", re.IGNORECASE),
]


def _redact_value(value: Any, depth: int = 0) -> Any:
    """递归脱敏：str 中的密钥模式替换为 ***；dict/list 递归。深度限制防循环。"""
    if depth > 6:
        return "<redacted-depth>"
    if isinstance(value, str):
        out = value
        for pat in _SECRET_PATTERNS:
            out = pat.sub("***", out)
        return out
    if isinstance(value, dict):
        return {k: _redact_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, depth + 1) for v in value]
    return value


def redact(value: Any) -> Any:
    """对外脱敏入口（供各观测点调用）。"""
    try:
        return _redact_value(value)
    except Exception:  # noqa: BLE001 脱敏失败不阻断业务
        return "<redact-error>"


def _client_enabled() -> bool:
    s = get_settings()
    return bool(s.langfuse_public_key and s.langfuse_secret_key)


def get_langfuse() -> Any:
    """返回 Langfuse 客户端或 None（无凭证时安全 no-op）。"""
    global _client
    if not _client_enabled():
        return None
    if _client is not None:
        return _client
    try:
        from langfuse import Langfuse  # 延迟导入：未安装时 no-op

        s = get_settings()
        _client = Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            host=s.langfuse_host or "https://cloud.langfuse.com",
        )
        return _client
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse 客户端初始化失败，观测禁用: %s", exc)
        return None


@contextmanager
def turn_trace(
    *,
    name: str,
    thread_id: str | None = None,
    actor_id: str | None = None,
    request_id: str | None = None,
    input_: Any = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> Iterator[Any]:
    """包裹一轮 Agent turn：创建 trace，结束时更新输出；错误标记 ERROR。

    无凭证时 yield None（调用方必须容忍 None）。
    """
    client = get_langfuse()
    if client is None:
        yield None
        return
    meta: dict = dict(metadata or {})
    if thread_id:
        meta["thread_id"] = thread_id
    if request_id:
        meta["request_id"] = request_id
    elif _request_id.get():
        meta["request_id"] = _request_id.get()
    trace = None
    try:
        trace = client.trace(
            name=name,
            session_id=thread_id,
            user_id=actor_id,
            input=redact(input_),
            metadata=meta,
            tags=tags or ["stockmind", "v1"],
        )
        _current_trace.set(trace)
        try:
            yield trace
        finally:
            if _current_trace.get() is trace:
                _current_trace.set(None)
    except Exception as exc:  # noqa: BLE001 观测失败不影响业务
        logger.warning("Langfuse trace 创建失败（忽略）: %s", exc)
        yield None


def current_trace() -> Any:
    """当前 turn 的 trace（无则 None）。"""
    return _current_trace.get()


def record_span(
    *,
    name: str,
    input_: Any = None,
    output: Any = None,
    metadata: dict | None = None,
    level: str | None = None,
    status_message: str | None = None,
) -> None:
    """在当前 trace 下记录一个 span（工具/领域计算等）；无 trace 或失败则静默。"""
    trace = _current_trace.get()
    if trace is None:
        return
    try:
        span = trace.span(
            name=name,
            input=redact(input_),
            output=redact(output),
            metadata=redact(metadata),
            level=level,
            status_message=status_message,
        )
        span.end()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse span 记录失败（忽略）: %s", exc)


def record_generation(
    *,
    name: str,
    model: str,
    input_: Any = None,
    output: Any = None,
    usage: dict | None = None,
    metadata: dict | None = None,
    level: str | None = None,
    status_message: str | None = None,
    start_time: Any = None,
    end_time: Any = None,
) -> None:
    """记录 LLM generation（模型、Token、耗时、状态）。

    Token 计数始终累计（无 Langfuse 凭证也累计，供本地评测采样）。
    """
    global _token_input, _token_output, _token_total
    if usage:
        _token_input += int(usage.get("input") or 0)
        _token_output += int(usage.get("output") or 0)
        _token_total += int(usage.get("total") or 0)
    trace = _current_trace.get()
    if trace is None:
        return
    try:
        kwargs: dict[str, Any] = {
            "name": name,
            "model": model,
            "input": redact(input_),
            "output": redact(output),
            "metadata": redact(metadata),
            "level": level,
            "status_message": status_message,
        }
        if start_time is not None:
            kwargs["start_time"] = start_time
        if end_time is not None:
            kwargs["end_time"] = end_time
        gen = trace.generation(**kwargs)
        if usage:
            try:
                from langfuse.model import ModelUsage

                gen.update(
                    usage=ModelUsage(
                        input=usage.get("input"),
                        output=usage.get("output"),
                        total=usage.get("total"),
                    )
                )
            except Exception:  # noqa: BLE001 usage 附加失败不致命
                gen.update(usage_details=redact(usage))
        gen.end()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse generation 记录失败（忽略）: %s", exc)


def mark_error(status_message: str) -> None:
    """把当前 trace 标记为 ERROR（错误观测；业务异常仍照常抛出）。"""
    trace = _current_trace.get()
    if trace is None:
        return
    try:
        trace.update(level="ERROR", status_message=redact(status_message)[:2000])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse 错误标记失败（忽略）: %s", exc)


def flush() -> None:
    """冲刷待发送观测（请求结束/进程退出前调用；失败静默）。"""
    client = get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse flush 失败（忽略）: %s", exc)
