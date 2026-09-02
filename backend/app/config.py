"""应用配置（Pydantic Settings，环境变量驱动）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 数据库（PostgreSQL + pgvector）
    postgres_dsn: str = "postgresql+psycopg://stockmind:stockmind@127.0.0.1:5432/stockmind"

    # Redis（Celery broker / 协调）
    redis_url: str = "redis://127.0.0.1:6379/0"

    # 模拟供应商
    mock_supplier_url: str = "http://127.0.0.1:8100"
    mock_supplier_store_path: str = ""

    # LLM（OpenAI 兼容；留空则进入 OFFLINE 演示模式）
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 60.0

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
