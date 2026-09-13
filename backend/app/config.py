"""应用配置（Pydantic Settings，环境变量驱动）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# LLM 运行模式白名单（见 ADR-007 决策 1）
LLM_MODES: tuple[str, ...] = ("auto", "llm", "offline")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 数据库（PostgreSQL + pgvector）
    postgres_dsn: str = "postgresql+psycopg://stockmind:stockmind@127.0.0.1:5432/stockmind"

    # Redis（Celery broker / 协调）
    redis_url: str = "redis://127.0.0.1:6379/0"

    # 模拟供应商
    mock_supplier_url: str = "http://127.0.0.1:8100"
    mock_supplier_store_path: str = ""

    # LLM（OpenAI 兼容；auto=已配置则优先真实 LLM，失败/缺配置自动降级 offline）
    llm_mode: str = "auto"  # auto / llm / offline
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 60.0
    # 假设单价，单位 USD / 1K tokens；未配置时只记录 Token，不虚构成本
    llm_price_per_1k_input: float | None = None
    llm_price_per_1k_output: float | None = None

    # Agent 防失控门禁
    agent_max_steps: int = 10
    agent_tool_duplicate_limit: int = 2

    # 本地中文 Embedding（RAG）
    embedding_model_name: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_enabled: bool = True
    embedding_dim: int = 384
    embedding_device: str = "cpu"

    # Langfuse 观测（可选）
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # 业务默认值
    default_timezone: str = "Asia/Shanghai"
    default_planning_window_days: int = 14
    advisory_lock_timeout_ms: int = 10_000
    order_timeout_seconds: int = 30
    business_day_rule: str = "latest_closed_day"  # 领域服务按仓库时区取业务日

    # 执行环境标识
    env: str = "dev"  # dev / test / prod

    # LangGraph checkpoint 后端：postgres（生产/compose）或 memory（开发/测试）
    checkpointer_backend: str = "postgres"

    @field_validator("llm_mode")
    @classmethod
    def _validate_llm_mode(cls, value: str) -> str:
        """``LLM_MODE`` 只允许 auto / llm / offline。

        空值按 ``auto`` 处理（兼容 .env 中留空的写法）；其他非法值在**配置加载时立即失败**，
        避免把拼写错误静默当成默认模式运行。
        """
        mode = (value or "").strip().lower() or "auto"
        if mode not in LLM_MODES:
            raise ValueError(f"LLM_MODE 只能是 {' / '.join(LLM_MODES)}，收到 {value!r}")
        return mode


@lru_cache
def get_settings() -> Settings:
    return Settings()
