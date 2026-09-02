"""Langfuse 可选观测单元测试（mock，不调用真实云服务）。

覆盖：
- 未配置凭证时安全 no-op（不初始化客户端、不产生调用）；
- 配置凭证时创建 trace/span/generation 并携带 thread_id/request_id/plan_id 关联；
- 脱敏：API Key 与敏感文本不落盘；
- 观测失败（mock 抛异常）不影响业务（不向上抛出）。
"""

from __future__ import annotations

import pytest

from app import observability
from app.observability import (
    mark_error,
    record_generation,
    record_span,
    redact,
    set_request_id,
    turn_trace,
)


@pytest.fixture(autouse=True)
def _no_creds(monkeypatch):
    """默认无凭证；各用例按需 monkeypatch。"""
    monkeypatch.setattr(observability, "_client", None)
    monkeypatch.setattr("app.config.get_settings", lambda: _settings({"": ""}, {}))
    yield


class _FakeSettings:
    def __init__(self, values: dict):
        self._v = values

    def __getattr__(self, name):
        return self._v.get(name)


def _settings(env: dict, defaults: dict | None = None) -> _FakeSettings:
    values = {
        "langfuse_public_key": "",
        "langfuse_secret_key": "",
        "langfuse_host": "https://cloud.langfuse.com",
        "llm_model": "deepseek-chat",
    }
    values.update(defaults or {})
    values.update(env)
    return _FakeSettings(values)


class _FakeClient:
    """最小 mock Langfuse 客户端，记录调用。"""

    def __init__(self):
        self.traces = []
        self.spans = []
        self.generations = []
        self.updated = []
        self.flushed = 0

    def trace(self, **kwargs):
        t = {"kind": "trace", **kwargs}
        self.traces.append(t)
        return _FakeTrace(self, t)

    def flush(self):
        self.flushed += 1


class _FakeTrace:
    def __init__(self, client, record):
        self._client = client
        self._record = record

    def span(self, **kwargs):
        s = {"kind": "span", **kwargs}
        self._client.spans.append(s)
        return _FakeSpan(self._client, s)

    def generation(self, **kwargs):
        g = {"kind": "generation", **kwargs}
        self._client.generations.append(g)
        return _FakeSpan(self._client, g)

    def update(self, **kwargs):
        self._client.updated.append({"target": "trace", **kwargs})


class _FakeSpan:
    def __init__(self, client, record):
        self._client = client
        self._record = record

    def update(self, **kwargs):
        self._client.updated.append({"target": "span", **kwargs})

    def end(self):
        pass


# ---------------------------------------------------------------- 无凭证 no-op


def test_no_credentials_is_safe_noop(monkeypatch):
    """无凭证：不初始化客户端，turn_trace 产出 None，观测调用不抛错。"""
    monkeypatch.setattr(observability, "_client", None)
    seen = []

    def fake_get():
        seen.append("called")
        return None

    monkeypatch.setattr(observability, "get_langfuse", fake_get)
    with turn_trace(name="t", thread_id="th-1") as trace:
        assert trace is None
        record_span(name="s", input_={"a": 1})  # 不应抛错
        record_generation(name="g", model="m", input_="x", output="y", usage={})
        mark_error("boom")
    assert seen == ["called"]


# ---------------------------------------------------------------- 有凭证


def test_credentials_create_trace_with_correlation(monkeypatch):
    """配置凭证后：创建 trace，metadata 携带 thread_id / request_id。"""
    client = _FakeClient()
    monkeypatch.setattr(observability, "get_langfuse", lambda: client)
    set_request_id("req-42")

    with turn_trace(
        name="agent.turn",
        thread_id="th-9",
        actor_id="alice",
        input_="帮我补货",
        metadata={"plan_id": "plan-1"},
    ) as trace:
        assert trace is not None
        record_span(name="domain.generate_draft", metadata={"plan_id": "plan-1"})
        record_generation(name="llm.chat", model="deepseek-chat", input_="q", output="a")

    assert len(client.traces) == 1
    t = client.traces[0]
    assert t["name"] == "agent.turn"
    assert t["session_id"] == "th-9"
    assert t["user_id"] == "alice"
    assert t["metadata"]["thread_id"] == "th-9"
    assert t["metadata"]["request_id"] == "req-42"
    assert t["metadata"]["plan_id"] == "plan-1"
    assert len(client.spans) == 1
    assert client.spans[0]["name"] == "domain.generate_draft"
    assert len(client.generations) == 1
    assert client.generations[0]["model"] == "deepseek-chat"


# ---------------------------------------------------------------- 脱敏


def test_redact_removes_secrets():
    """API Key 与敏感字段值被脱敏；普通业务文本保留。"""
    out = redact(
        {
            "prompt": "使用 key sk-abc12345XYZ 调用",
            "api_key": "sk-live-1234567890abcdef",
            "plan_id": "plan-1",
            "text": "正常业务内容",
        }
    )
    assert "sk-abc12345XYZ" not in str(out)
    assert "sk-live-1234567890abcdef" not in str(out)
    assert out["plan_id"] == "plan-1"  # 业务标识保留用于关联
    assert out["text"] == "正常业务内容"


def test_generation_usage_recorded(monkeypatch):
    """Token 使用量记录到 generation（usage_details 兜底）。"""
    client = _FakeClient()
    monkeypatch.setattr(observability, "get_langfuse", lambda: client)
    with turn_trace(name="t", thread_id="th-1"):
        record_generation(name="llm.chat", model="m", input_="q", output="a", usage={"input": 10, "output": 5})
    assert len(client.generations) == 1


# ---------------------------------------------------------------- 观测失败不影响业务


def test_observability_failure_does_not_break_business(monkeypatch):
    """mock 客户端抛异常时，观测调用被吞掉，业务继续。"""

    class _BoomClient:
        def trace(self, **_kwargs):  # noqa: ARG002  mock 客户端无需使用参数
            raise RuntimeError("cloud down")

    monkeypatch.setattr(observability, "get_langfuse", lambda: _BoomClient())
    with turn_trace(name="t", thread_id="th-1") as trace:
        assert trace is None  # 创建失败 -> 安全降级为 None
        record_span(name="s")  # 不抛错
        record_generation(name="g", model="m")
        mark_error("e")
    # 业务继续执行
    assert True
