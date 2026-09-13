"""SSE 流式事件缓冲、断线续传与补流（需求 8.2 增强 / 架构 3）。

真流式改造后，Agent 一轮对话的节点级事件被逐条推送。本模块负责：

- 为事件分配单调递增序号（``id: turn_id:seq``）并缓存最近若干轮的事件序列；
- 支持客户端断线后携带 ``Last-Event-ID`` 续传：**只重放尚未收到的进度，不重新执行
  Agent**，从而避免重复调用 ``generate_draft`` 触发防重副作用（流式层幂等）；
- 缓冲丢失（进程重启 / 容量淘汰）时回退 LangGraph checkpoint 读取最终状态并重放
  最小事件集；无法恢复时也发出 ``error`` + ``done``，保证客户端不会永久等待。

本缓冲是进程内、有容量上限的**流式传输辅助层**，不是业务事实源；业务状态仍以业务
数据库为准（宪法第十二条），checkpoint 只是流程恢复依据。

会话（``conversations``）与 Agent 主链路（``agent``）两个入口共用这里的实现。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("stockmind.streaming")

# 最多缓存最近 N 轮对话的事件序列；超出淘汰最旧（进程内、非业务事实源）
_MAX_BUFFERED_TURNS = 256

# SSE 事件字段顺序：id 行在前，客户端据此解析并记录 Last-Event-ID
_SSE_TEMPLATE = "id: {turn_id}:{seq}\nevent: {event}\ndata: {data}\n\n"


def sse_event(turn_id: str, seq: int, event: str, data: dict) -> str:
    """把事件渲染为带序号（id 行）的 SSE 帧。"""
    return _SSE_TEMPLATE.format(
        turn_id=turn_id,
        seq=seq,
        event=event,
        data=json.dumps(data, ensure_ascii=False),
    )


def parse_last_event_id(value: str | None) -> tuple[str, int] | None:
    """解析 ``Last-Event-ID``（``turn_id:seq``）。

    非法输入一律返回 None：空值、缺少分隔符、空 turn_id、非整数 seq、负数 seq 都会被拒绝。
    调用方应先经 :func:`classify_last_event_id` 区分「无游标」与「游标非法」，前者是正常
    新请求，后者必须显式拒绝。
    """
    if not value:
        return None
    turn_id, sep, seq_part = value.partition(":")
    if not sep or not turn_id or not seq_part:
        return None
    try:
        seq = int(seq_part)
    except ValueError:
        return None
    if seq < 0:
        return None
    return turn_id, seq


def classify_last_event_id(value: str | None) -> tuple[str, tuple[str, int] | None]:
    """分类 ``Last-Event-ID``，返回 ``(status, parsed)``。

    - ``absent``：没有该请求头（或全空白）——正常的**新请求**，应执行新任务；
    - ``valid``：格式正确——进入断线续传路径；
    - ``invalid``：存在但格式非法——**必须显式拒绝（稳定 4xx）**。

    非法游标绝不能被当作"无游标"而静默开启新任务：客户端以为在续传，实际却重新执行了
    一轮 Agent，可能重复调用 ``generate_draft`` 并产生重复副作用。
    """
    if value is None or not str(value).strip():
        return "absent", None
    parsed = parse_last_event_id(value)
    if parsed is None:
        return "invalid", None
    return "valid", parsed


class _TurnLog:
    __slots__ = ("turn_id", "thread_id", "events", "done")

    def __init__(self, turn_id: str, thread_id: str):
        self.turn_id = turn_id
        self.thread_id = thread_id
        self.events: list[tuple[int, str, dict]] = []  # (seq, event, data)
        self.done = False


class StreamBuffer:
    """进程内事件缓冲：按 turn_id 缓存事件序列，支持断线续传。

    线程安全（append / replay 分别加锁）；缓冲属于传输辅助层，不做跨进程持久化。
    """

    def __init__(self, max_turns: int = _MAX_BUFFERED_TURNS):
        self._max_turns = max_turns
        self._lock = threading.Lock()
        self._turns: OrderedDict[str, _TurnLog] = OrderedDict()

    def append(self, thread_id: str, turn_id: str, seq: int, event: str, data: dict) -> None:
        with self._lock:
            log = self._turns.get(turn_id)
            if log is None:
                log = _TurnLog(turn_id, thread_id)
                self._turns[turn_id] = log
                self._turns.move_to_end(turn_id)
                # 淘汰最旧，防止内存膨胀
                while len(self._turns) > self._max_turns:
                    self._turns.popitem(last=False)
            log.events.append((seq, event, data))
            if event == "done":
                log.done = True

    def replay_after(self, thread_id: str, turn_id: str, after_seq: int) -> list[tuple[int, str, dict]] | None:
        """返回 turn_id 缓冲中 seq > after_seq 的事件。

        缓冲不存在或 turn 不属于该 thread_id（防越权重放）时返回 None。
        """
        with self._lock:
            log = self._turns.get(turn_id)
            if log is None or log.thread_id != thread_id:
                return None
            return [(s, e, d) for (s, e, d) in log.events if s > after_seq]

    def is_complete(self, turn_id: str) -> bool:
        with self._lock:
            log = self._turns.get(turn_id)
            return bool(log and log.done)

    def read_turn(self, thread_id: str, turn_id: str, after_seq: int) -> tuple[str, list[tuple[int, str, dict]]]:
        """原子读取 turn 的重放状态与待重放事件（避免 replay 与完成判定之间的竞态）。

        返回 ``(status, events)``；``status`` 取值：

        - ``complete``：turn 属于该 thread 且已产生 ``done``；
        - ``incomplete``：turn 属于该 thread 但尚未完成；
        - ``missing``：缓冲中没有该 turn（进程重启 / 容量淘汰）；
        - ``foreign``：该 turn 存在但属于其他 thread（拒绝越权重放）。
        """
        with self._lock:
            log = self._turns.get(turn_id)
            if log is None:
                return "missing", []
            if log.thread_id != thread_id:
                return "foreign", []
            events = [(s, e, d) for (s, e, d) in log.events if s > after_seq]
            return ("complete" if log.done else "incomplete"), events


class RedisStreamBuffer:
    """Redis 传输事件缓冲。

    Redis 只保存短期 SSE 传输事件，不能替代业务表或 checkpoint。事件按 seq 存在
    Hash 中，重复 append 使用同一 seq 覆盖，进程重启或多 API 进程切换后仍可续传。
    Redis 不可用时由 :func:`_build_buffer` 回退到进程内 ``StreamBuffer``。
    """

    _INDEX_KEY = "stockmind:sse:index"

    def __init__(self, client: Any, *, ttl_seconds: int = 3600, max_turns: int = 256, max_events: int = 512):
        self._client = client
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_turns = max(1, int(max_turns))
        self._max_events = max(1, int(max_events))
        self._lock = threading.Lock()

    @staticmethod
    def _meta_key(turn_id: str) -> str:
        return f"stockmind:sse:{turn_id}:meta"

    @staticmethod
    def _events_key(turn_id: str) -> str:
        return f"stockmind:sse:{turn_id}:events"

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    def _trim_turn(self, turn_id: str) -> None:
        events_key = self._events_key(turn_id)
        fields = [self._text(field) for field in self._client.hkeys(events_key)]
        if len(fields) <= self._max_events:
            return
        old = sorted(fields, key=lambda value: int(value))[: len(fields) - self._max_events]
        if old:
            self._client.hdel(events_key, *old)

    def _trim_turns(self) -> None:
        turn_ids = [self._text(value) for value in self._client.zrange(self._INDEX_KEY, 0, -1)]
        excess = len(turn_ids) - self._max_turns
        if excess <= 0:
            return
        for turn_id in turn_ids[:excess]:
            self._client.delete(self._meta_key(turn_id), self._events_key(turn_id))
            self._client.zrem(self._INDEX_KEY, turn_id)

    def append(self, thread_id: str, turn_id: str, seq: int, event: str, data: dict) -> None:
        with self._lock:
            meta_key = self._meta_key(turn_id)
            events_key = self._events_key(turn_id)
            existing_thread = self._client.hget(meta_key, "thread_id")
            if existing_thread is not None and self._text(existing_thread) != thread_id:
                logger.warning("拒绝写入属于其他 thread 的 SSE turn: %s", turn_id)
                return
            done = self._client.hget(meta_key, "done")
            done_value = "1" if event == "done" or self._text(done or "0") == "1" else "0"
            self._client.hset(meta_key, mapping={"thread_id": thread_id, "done": done_value})
            self._client.hset(
                events_key,
                str(seq),
                json.dumps({"seq": seq, "event": event, "data": data}, ensure_ascii=False),
            )
            self._client.expire(meta_key, self._ttl_seconds)
            self._client.expire(events_key, self._ttl_seconds)
            self._client.zadd(self._INDEX_KEY, {turn_id: time.time()})
            self._client.expire(self._INDEX_KEY, self._ttl_seconds)
            self._trim_turn(turn_id)
            self._trim_turns()

    def read_turn(self, thread_id: str, turn_id: str, after_seq: int) -> tuple[str, list[tuple[int, str, dict]]]:
        meta = self._client.hgetall(self._meta_key(turn_id))
        if not meta:
            return "missing", []
        stored_thread = self._text(meta.get("thread_id", ""))
        if stored_thread != thread_id:
            return "foreign", []
        events: list[tuple[int, str, dict]] = []
        for raw in self._client.hvals(self._events_key(turn_id)):
            try:
                item = json.loads(self._text(raw))
                seq = int(item["seq"])
                if seq > after_seq:
                    events.append((seq, str(item["event"]), dict(item.get("data") or {})))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                logger.warning("忽略损坏的 Redis SSE 事件: %s", turn_id)
        events.sort(key=lambda item: item[0])
        done = self._text(meta.get("done", "0")) == "1"
        return ("complete" if done else "incomplete"), events

    def replay_after(self, thread_id: str, turn_id: str, after_seq: int) -> list[tuple[int, str, dict]] | None:
        status, events = self.read_turn(thread_id, turn_id, after_seq)
        return None if status in {"missing", "foreign"} else events

    def is_complete(self, turn_id: str) -> bool:
        value = self._client.hget(self._meta_key(turn_id), "done")
        return self._text(value or "0") == "1"


_buffer: StreamBuffer | RedisStreamBuffer | None = None


def _build_buffer() -> StreamBuffer | RedisStreamBuffer:
    """按配置创建传输缓冲；Redis 不可用时安全回退内存。"""
    from app.config import get_settings

    settings = get_settings()
    if settings.stream_event_backend.strip().lower() != "redis":
        return StreamBuffer(max_turns=settings.stream_event_max_turns)
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        return RedisStreamBuffer(
            client,
            ttl_seconds=settings.stream_event_ttl_seconds,
            max_turns=settings.stream_event_max_turns,
            max_events=settings.stream_event_max_events,
        )
    except Exception as exc:  # noqa: BLE001 传输层不可用时不影响业务流
        logger.warning("Redis SSE 事件后端不可用，回退进程内缓冲: %s", exc)
        return StreamBuffer(max_turns=settings.stream_event_max_turns)


def get_buffer() -> StreamBuffer | RedisStreamBuffer:
    """返回配置的全局事件缓冲（内存或 Redis）。"""
    global _buffer
    if _buffer is None:
        _buffer = _build_buffer()
    return _buffer


async def _emit_sse(*, thread_id: str, turn_id: str, request_id: str, source):
    """把 ``(event, data)`` 异步源渲染为 SSE 帧并写入缓冲。

    异常统一转为 ``error`` + ``done``，保证客户端不会永久等待。
    """
    from app.errors import StockMindError
    from app.observability import mark_error, set_request_id

    set_request_id(request_id)
    buffer = get_buffer()
    seq = 0

    def emit(event: str, data: dict) -> str:
        nonlocal seq
        buffer.append(thread_id, turn_id, seq, event, data)
        frame = sse_event(turn_id, seq, event, data)
        seq += 1
        return frame

    try:
        async for event, data in source:
            yield emit(event, data)
    except StockMindError as exc:
        mark_error(f"{exc.code}: {exc.message}")
        yield emit("error", {"code": exc.code, "message": exc.message})
        yield emit("done", {"recovered": False})
    except Exception as exc:  # noqa: BLE001 传输层兜底：转 error + done，不吞掉日志
        mark_error(f"{type(exc).__name__}: {exc}")
        logger.exception("agent turn failed")
        yield emit("error", {"code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}"})
        yield emit("done", {"recovered": False})


async def stream_agent_turn(*, thread_id: str, turn_id: str, content: str, actor_id: str, request_id: str):
    """执行一轮新会话并逐事件渲染为 SSE 帧（写入缓冲供断线续传）。

    只负责传输层；Agent 的业务语义与降级原因由 ``app.agent.graph.astream_turn`` 提供。
    """
    from app.agent.graph import astream_turn

    async for frame in _emit_sse(
        thread_id=thread_id,
        turn_id=turn_id,
        request_id=request_id,
        source=astream_turn(thread_id, actor_id, content),
    ):
        yield frame


async def stream_agent_resume(
    *, thread_id: str, turn_id: str, plan_id: str, decision_version: int, request_id: str
):
    """恢复一轮会话（读取数据库已提交决定）并渲染为 SSE 帧。

    **不执行审批、下单或任何业务副作用**；业务状态始终以数据库为准。
    """
    from app.agent.graph import astream_resume

    async for frame in _emit_sse(
        thread_id=thread_id,
        turn_id=turn_id,
        request_id=request_id,
        source=astream_resume(thread_id, plan_id, decision_version),
    ):
        yield frame


def _checkpoint_fallback_frames(thread_id: str) -> list[tuple[str, dict]]:
    """缓冲丢失时从 LangGraph checkpoint 读最终态，重放最小事件集（message + done）。

    状态不可恢复时返回 ``error`` + ``done``，保证调用方总能得到终止事件。
    """
    from app.agent.graph import get_graph

    try:
        snapshot = get_graph().get_state({"configurable": {"thread_id": thread_id}})
        state = (snapshot.values if snapshot else None) or {}
    except Exception as exc:  # noqa: BLE001 缓冲与 checkpoint 均不可用
        logger.warning("checkpoint 兜底读取失败: %s", exc)
        state = {}
    response = state.get("response", "")
    if not response:
        return [
            ("error", {"code": "TURN_NOT_FOUND", "message": "对话状态不可恢复，请重新发起"}),
            ("done", {"recovered": False}),
        ]
    return [
        (
            "message",
            {
                "content": response,
                "offline": state.get("offline", True),
                "degradation_reason": state.get("degradation_reason"),
                "step_count": state.get("step_count"),
                "loop_blocked": bool(state.get("loop_blocked")),
                "outcome": state.get("outcome"),
                "blocked_lines": state.get("blocked_lines"),
                "missing_params": state.get("missing_params"),
            },
        ),
        (
            "done",
            {
                "interrupted": bool(state.get("needs_approval")),
                "plan_id": state.get("plan_id"),
                "outcome": state.get("outcome"),
                "step_count": state.get("step_count"),
                "loop_blocked": bool(state.get("loop_blocked")),
                "degradation_reason": state.get("degradation_reason"),
                "recovered": True,
            },
        ),
    ]


def sse_replay_frames(*, thread_id: str, turn_id: str, after_seq: int):
    """断线续传：只重放缓冲中 seq > after_seq 的已有事件，**不重新执行 Agent**。

    通过 ``StreamBuffer.read_turn`` 原子读取状态并区分四种情况：

    - ``complete``：turn 已完成（含 ``after_seq`` 已指向 ``done`` 或超出 ``done_seq``）——
      只重放余量后结束，**不再读取 checkpoint**，因此不会重复发送 ``message`` / ``done``；
    - ``incomplete``：turn 存在但未完成——重放余量后用 checkpoint 补最小事件集；
    - ``missing``：缓冲中没有该 turn（进程重启 / 容量淘汰）——直接用 checkpoint 兜底；
    - ``foreign``：该 turn 不属于该 thread——返回稳定 ``error`` + ``done``，不读 checkpoint。

    补流事件序号自 ``after_seq`` 起单调递增；所有路径都以 ``done`` 结尾（客户端不会永久等待）。
    本函数只读缓冲与 checkpoint，不执行图、不调用 ``generate_draft``，因此不会产生副作用。
    """
    buffer = get_buffer()
    status, buffered = buffer.read_turn(thread_id, turn_id, after_seq)
    seq = after_seq

    if status == "foreign":
        # 越权/不匹配的重放请求：不泄漏任何事件，也不读 checkpoint
        seq += 1
        yield sse_event(
            turn_id,
            seq,
            "error",
            {"code": "TURN_NOT_FOUND", "message": "该轮次不存在或不属于当前会话"},
        )
        seq += 1
        yield sse_event(turn_id, seq, "done", {"recovered": False})
        return

    for _, event, data in buffered:
        seq += 1
        yield sse_event(turn_id, seq, event, data)

    if status == "complete":
        # 已完成（包括 after_seq 已指向 done 或超出）：不得再补流、不得再读 checkpoint
        return

    # incomplete / missing：用 checkpoint 兜底补最小事件集（含 done）
    saw_done = False
    for event, data in _checkpoint_fallback_frames(thread_id):
        seq += 1
        yield sse_event(turn_id, seq, event, data)
        if event == "done":
            saw_done = True
    if not saw_done:  # pragma: no cover 防御：兜底必须给出终止事件
        seq += 1
        yield sse_event(turn_id, seq, "done", {"recovered": False})
