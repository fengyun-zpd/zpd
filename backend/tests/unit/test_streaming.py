"""SSE 流式缓冲、续传与补流语义单元测试（真流式 + 断线续传，需求 8.2 增强）。"""

from __future__ import annotations

import pytest

from app import streaming
from app.streaming import StreamBuffer, classify_last_event_id, parse_last_event_id, sse_event


def _events(frames: list[str]) -> list[str]:
    """从 SSE 帧中提取事件名。"""
    out = []
    for frame in frames:
        for line in frame.split("\n"):
            if line.startswith("event: "):
                out.append(line[len("event: ") :])
    return out


def test_sse_event_includes_id_line():
    frame = sse_event("turn-1", 3, "message", {"content": "你好"})
    assert frame.startswith("id: turn-1:3\n")
    assert "event: message\n" in frame
    assert '"content": "你好"' in frame


# ---------------------------------------------------------------- Last-Event-ID 校验


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "abc",  # 缺分隔符
        ":5",  # 空 turn_id
        "abc:",  # 空 seq
        "abc:xyz",  # 非整数 seq
        "abc:-1",  # 负数 seq
        "abc:-100",
        "abc:1.5",
        ":",
    ],
)
def test_parse_last_event_id_rejects_invalid(value):
    """非法 Last-Event-ID 一律按"无游标"处理（空 turn_id / 负数 seq / 非法格式）。"""
    assert parse_last_event_id(value) is None


def test_parse_last_event_id_accepts_valid():
    assert parse_last_event_id("turn-1:0") == ("turn-1", 0)
    assert parse_last_event_id("turn-1:42") == ("turn-1", 42)


# ---------------------------------------------------------------- Last-Event-ID 三态


@pytest.mark.parametrize("value", [None, "", "   "])
def test_classify_absent_when_no_cursor(value):
    """无游标（无请求头或全空白）是正常新请求。"""
    assert classify_last_event_id(value) == ("absent", None)


@pytest.mark.parametrize("value", ["turn-1:0", "turn-1:42"])
def test_classify_valid_cursor(value):
    status, parsed = classify_last_event_id(value)
    assert status == "valid"
    assert parsed == parse_last_event_id(value)


@pytest.mark.parametrize("value", ["abc", ":5", "abc:", "abc:xyz", "abc:-1", ":", "abc:1.5"])
def test_classify_invalid_cursor_rejected(value):
    """存在但非法的游标必须标记为 invalid（调用方需返回稳定 4xx，不得静默开新任务）。"""
    assert classify_last_event_id(value) == ("invalid", None)


# ---------------------------------------------------------------- 缓冲


def test_buffer_replay_after_seq():
    buf = StreamBuffer()
    buf.append("t1", "turn-1", 0, "agent_start", {})
    buf.append("t1", "turn-1", 1, "message", {"content": "a"})
    buf.append("t1", "turn-1", 2, "done", {})
    events = buf.replay_after("t1", "turn-1", 0)
    assert events is not None
    assert [e for _, e, _ in events] == ["message", "done"]
    assert buf.is_complete("turn-1") is True


def test_buffer_replay_missing_turn_returns_none():
    buf = StreamBuffer()
    assert buf.replay_after("t1", "nope", 0) is None
    assert buf.is_complete("nope") is False


def test_buffer_replay_cross_thread_blocked():
    """补流必须校验 turn 归属 thread_id，防止越权重放他会话事件。"""
    buf = StreamBuffer()
    buf.append("alice-thread", "turn-1", 0, "message", {"content": "secret"})
    assert buf.replay_after("mallory-thread", "turn-1", 0) is None


# ---------------------------------------------------------------- 补流语义


def _seqs(frames: list[str]) -> list[int]:
    """从 SSE 帧的 ``id:`` 行提取序号。"""
    out: list[int] = []
    for frame in frames:
        for line in frame.split("\n"):
            if line.startswith("id: "):
                out.append(int(line.rsplit(":", 1)[-1]))
    return out


def _no_fallback(monkeypatch) -> dict:
    """把 checkpoint 兜底替换为打桩（记录是否被调用）。"""
    calls = {"count": 0}

    def _fake(_thread_id: str):
        calls["count"] += 1
        return [("message", {"content": "fallback"}), ("done", {"recovered": True})]

    monkeypatch.setattr(streaming, "_checkpoint_fallback_frames", _fake)
    return calls


def _buffer_with_done(monkeypatch) -> StreamBuffer:
    """含 agent_start + message + done 的已完成 turn（done 位于 seq=2）。"""
    buf = StreamBuffer()
    buf.append("t1", "turn-1", 0, "agent_start", {})
    buf.append("t1", "turn-1", 1, "message", {"content": "hi"})
    buf.append("t1", "turn-1", 2, "done", {})
    monkeypatch.setattr(streaming, "_buffer", buf)
    return buf


def test_replay_at_done_seq_returns_no_events(monkeypatch):
    """after_seq 已指向 done：返回空事件，且**不得**读取 checkpoint 补流。"""
    _buffer_with_done(monkeypatch)
    calls = _no_fallback(monkeypatch)

    frames = list(streaming.sse_replay_frames(thread_id="t1", turn_id="turn-1", after_seq=2))
    assert frames == []
    assert calls["count"] == 0, "已指向 done 时不得走 checkpoint 兜底"


def test_replay_beyond_done_seq_returns_no_events(monkeypatch):
    """after_seq 超出 done_seq：同样不重复补流。"""
    _buffer_with_done(monkeypatch)
    calls = _no_fallback(monkeypatch)

    frames = list(streaming.sse_replay_frames(thread_id="t1", turn_id="turn-1", after_seq=99))
    assert frames == []
    assert calls["count"] == 0


def test_replay_complete_with_remaining_events(monkeypatch):
    """已完成但仍有未收事件：只重放余量，不追加 checkpoint 兜底。"""
    _buffer_with_done(monkeypatch)
    calls = _no_fallback(monkeypatch)

    frames = list(streaming.sse_replay_frames(thread_id="t1", turn_id="turn-1", after_seq=0))
    assert _events(frames) == ["message", "done"]
    assert calls["count"] == 0, "已完成 turn 不得走兜底"


def test_replay_foreign_turn_returns_stable_error(monkeypatch):
    """turn 属于其他 thread：返回稳定 error + done，且不读 checkpoint（不越权重放）。"""
    buf = StreamBuffer()
    buf.append("alice-thread", "turn-1", 0, "message", {"content": "secret"})
    buf.append("alice-thread", "turn-1", 1, "done", {})
    monkeypatch.setattr(streaming, "_buffer", buf)
    calls = _no_fallback(monkeypatch)

    frames = list(streaming.sse_replay_frames(thread_id="mallory-thread", turn_id="turn-1", after_seq=0))
    assert _events(frames) == ["error", "done"]
    assert "secret" not in "".join(frames), "不得泄漏他会话事件"
    assert calls["count"] == 0, "越权重放不得读 checkpoint"


def test_replay_missing_turn_uses_fallback(monkeypatch):
    """turn 不存在（进程重启/淘汰）：用 checkpoint 兜底。"""
    monkeypatch.setattr(streaming, "_buffer", StreamBuffer())
    calls = _no_fallback(monkeypatch)

    frames = list(streaming.sse_replay_frames(thread_id="t1", turn_id="gone", after_seq=3))
    assert _events(frames) == ["message", "done"]
    assert calls["count"] == 1
    assert _seqs(frames) == [4, 5], "序号必须从 after_seq 起单调递增"


def test_replay_partial_buffer_appends_fallback(monkeypatch):
    """turn 存在但未完成：重放已有事件后补 checkpoint 最小事件集，序号单调递增。"""
    buf = StreamBuffer()
    buf.append("t1", "turn-1", 0, "agent_start", {})
    buf.append("t1", "turn-1", 1, "node_end", {"node": "classify"})
    monkeypatch.setattr(streaming, "_buffer", buf)
    monkeypatch.setattr(
        streaming,
        "_checkpoint_fallback_frames",
        lambda _thread_id: [("message", {"content": "final"}), ("done", {"recovered": True})],
    )

    frames = list(streaming.sse_replay_frames(thread_id="t1", turn_id="turn-1", after_seq=0))
    assert _events(frames) == ["node_end", "message", "done"]
    assert _seqs(frames) == [1, 2, 3]


def test_replay_fallback_always_terminates(monkeypatch):
    """缓冲缺失且 checkpoint 不可恢复：仍以 error + done 结束，客户端不会永久等待。"""
    monkeypatch.setattr(streaming, "_buffer", StreamBuffer())
    monkeypatch.setattr(
        streaming,
        "_checkpoint_fallback_frames",
        lambda _thread_id: [
            ("error", {"code": "TURN_NOT_FOUND", "message": "不可恢复"}),
            ("done", {"recovered": False}),
        ],
    )
    frames = list(streaming.sse_replay_frames(thread_id="t1", turn_id="nope", after_seq=5))
    assert _events(frames) == ["error", "done"]


def test_replay_never_calls_write_tool(monkeypatch):
    """续传只读缓冲与 checkpoint：不得重新执行 Agent，也不得再次调用 generate_draft。"""
    from app.agent import tools as agent_tools

    called = {"count": 0}
    original = agent_tools.generate_draft

    def _spy(**kwargs):
        called["count"] += 1
        return original(**kwargs)

    monkeypatch.setattr(agent_tools, "generate_draft", _spy)

    buf = StreamBuffer()
    buf.append("t1", "turn-1", 0, "agent_start", {})
    buf.append("t1", "turn-1", 1, "draft_created", {"plan_id": "p1"})
    buf.append("t1", "turn-1", 2, "done", {})
    monkeypatch.setattr(streaming, "_buffer", buf)

    frames = list(streaming.sse_replay_frames(thread_id="t1", turn_id="turn-1", after_seq=0))
    assert _events(frames) == ["draft_created", "done"]
    assert called["count"] == 0, "续传不得再次调用写工具"


def test_read_turn_reports_atomic_status():
    """read_turn 原子返回状态：missing / foreign / incomplete / complete。"""
    buf = StreamBuffer()
    assert buf.read_turn("t1", "nope", 0) == ("missing", [])

    buf.append("t1", "turn-1", 0, "agent_start", {})
    status, events = buf.read_turn("t1", "turn-1", 0)
    assert status == "incomplete"
    assert [e for _, e, _ in events] == []

    buf.append("t1", "turn-1", 1, "done", {})
    status, events = buf.read_turn("t1", "turn-1", 0)
    assert status == "complete"
    assert [e for _, e, _ in events] == ["done"]

    status, events = buf.read_turn("other-thread", "turn-1", 0)
    assert status == "foreign"
    assert events == []
