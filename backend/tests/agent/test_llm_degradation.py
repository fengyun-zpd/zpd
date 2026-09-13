"""真实 LLM 模式判定与降级原因测试（无 DB，需求 3 扩展）。

约束：``LLM_MODE=offline`` 强制不调用外部模型；``auto`` 配置完整才优先真实 LLM；
``llm`` 配置不完整时进入明确降级（``LLM_NOT_CONFIGURED``），不假装调用成功。
降级原因必须是稳定 code，供状态、SSE、日志与评测报告统一记录。
"""

from __future__ import annotations

import pytest

from app.agent.graph import _llm_configured, _llm_enabled, _llm_mode
from app.agent.llm import (
    LLMInvalidResponseError,
    LLMNotConfiguredError,
    LLMTimeoutError,
    LLMUnavailableError,
    classify_llm_error,
)
from app.config import get_settings

_FULL_CONFIG = {
    "LLM_API_KEY": "sk-test-key",
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "deepseek-chat",
}


def _set_env(monkeypatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_offline_mode_never_calls_llm(monkeypatch):
    """LLM_MODE=offline：即使配置完整也强制不调用外部模型。"""
    _set_env(monkeypatch, LLM_MODE="offline", **_FULL_CONFIG)
    try:
        assert _llm_mode() == "offline"
        assert _llm_enabled() is False
    finally:
        get_settings.cache_clear()


def test_auto_mode_without_key_not_enabled(monkeypatch):
    """auto 且缺 Key：不发起请求（由 node_classify 记录 LLM_NOT_CONFIGURED）。"""
    _set_env(monkeypatch, LLM_MODE="auto", LLM_API_KEY="")
    try:
        assert _llm_configured() is False
        assert _llm_enabled() is False
    finally:
        get_settings.cache_clear()


def test_auto_mode_with_full_config_enabled(monkeypatch):
    """auto 且配置完整：优先尝试真实 LLM。"""
    _set_env(monkeypatch, LLM_MODE="auto", **_FULL_CONFIG)
    try:
        assert _llm_configured() is True
        assert _llm_enabled() is True
    finally:
        get_settings.cache_clear()


def test_llm_mode_without_full_config_not_enabled(monkeypatch):
    """LLM_MODE=llm 但配置不完整：进入明确降级，不假装调用成功。"""
    _set_env(monkeypatch, LLM_MODE="llm", LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL="")
    try:
        assert _llm_mode() == "llm"
        assert _llm_enabled() is False
    finally:
        get_settings.cache_clear()


def test_llm_mode_requires_all_three_settings(monkeypatch):
    """三个配置项缺一即视为未配置（避免半配置下发起请求）。"""
    for missing in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        partial = dict(_FULL_CONFIG)
        partial[missing] = ""
        _set_env(monkeypatch, LLM_MODE="auto", **partial)
        try:
            assert _llm_configured() is False, f"缺少 {missing} 时不应视为已配置"
        finally:
            get_settings.cache_clear()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("timed out"), "LLM_TIMEOUT"),
        (Exception("Request timed out."), "LLM_TIMEOUT"),
        (RuntimeError("connection reset by peer"), "LLM_UNAVAILABLE"),
        (LLMNotConfiguredError("missing key"), "LLM_NOT_CONFIGURED"),
        (LLMInvalidResponseError("bad json"), "LLM_INVALID_RESPONSE"),
    ],
)
def test_classify_llm_error_maps_stable_code(exc, expected):
    assert classify_llm_error(exc).code == expected


def test_classify_llm_error_preserves_llm_error_instance():
    original = LLMTimeoutError("boom")
    assert classify_llm_error(original) is original


def test_llm_error_codes_are_stable():
    """降级原因是评测与 SSE 共用的稳定 code，不得随意改名。"""
    assert LLMNotConfiguredError.code == "LLM_NOT_CONFIGURED"
    assert LLMTimeoutError.code == "LLM_TIMEOUT"
    assert LLMInvalidResponseError.code == "LLM_INVALID_RESPONSE"
    assert LLMUnavailableError.code == "LLM_UNAVAILABLE"


# ---------------------------------------------------------------- LLM_MODE 白名单


def test_invalid_llm_mode_fails_at_config_load(monkeypatch):
    """非法 LLM_MODE 必须在配置加载时立即失败，而不是静默按 auto 运行。"""
    from pydantic import ValidationError

    monkeypatch.setenv("LLM_MODE", "sandbox")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_empty_llm_mode_falls_back_to_auto(monkeypatch):
    """空 LLM_MODE（.env 中留空）按 auto 处理，不报错。"""
    monkeypatch.setenv("LLM_MODE", "")
    get_settings.cache_clear()
    try:
        assert get_settings().llm_mode == "auto"
        assert _llm_mode() == "auto"
    finally:
        get_settings.cache_clear()


def test_llm_mode_is_normalized(monkeypatch):
    """LLM_MODE 允许大小写与空白差异，规范化为小写后使用。"""
    monkeypatch.setenv("LLM_MODE", "  OFFLINE  ")
    get_settings.cache_clear()
    try:
        assert get_settings().llm_mode == "offline"
        assert _llm_mode() == "offline"
        assert _llm_enabled() is False
    finally:
        get_settings.cache_clear()
