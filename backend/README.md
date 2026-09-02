# StockMind Backend

StockMind V1 后端：FastAPI + SQLAlchemy + Celery + LangGraph + PostgreSQL/pgvector。

详见仓库根目录 README 与四份基础设计文档。

## 依赖与可复现安装（双锁文件）

- `pyproject.toml`：声明依赖范围（`[project].dependencies`）与可选组（`rag` / `dev`）。
- `requirements.lock`：base + dev 精确版本锁（CI / 本机测试）。
  生成：`.dev/gen_lock.sh`（`pip-compile --extra dev --strip-extras`）。
  使用：`pip install -e ".[dev]" --constraint requirements.lock`。
- `requirements-rag.lock`：base + rag 精确版本锁（Docker 镜像；torch 固定 CPU 版）。
  生成：`.dev/gen_rag_lock.sh`（`pip-compile --extra rag --extra-index-url https://download.pytorch.org/whl/cpu
  --constraint /tmp/torch-cpu-constraint.txt`，其中 constraint 为 `torch==2.6.0+cpu`）。
  关键：以官方 CPU 源 + CPU torch constraint 解析依赖闭包，lock 内 `torch==2.6.0+cpu` 且
  **不含 nvidia/triton/cuda-*** 依赖；避免基于 PyPI 最新 CUDA torch 解析导致安装冲突或镜像膨胀。
  使用：先装 `torch==2.6.0+cpu`（官方 CPU 源），再
  `pip install --extra-index-url https://download.pytorch.org/whl/cpu --constraint requirements-rag.lock ".[rag]"`。
- torch：本仓库仅使用 CPU Embedding，固定官方 CPU wheel 源 `torch==2.6.0+cpu`
  （`download.pytorch.org/whl/cpu`，Python 3.11 官方 CPU wheel 最新可用版本），不安装 CUDA 运行时。
- 工具：pip / pip-tools（pip 生态，不强制 uv）。两套 lock 的生成命令见 `.dev/gen_lock.sh` / `.dev/gen_rag_lock.sh`。
- 迁移：`alembic/versions/3fd22d8de190_initial_schema.py` 的 `upgrade()` 内执行
  `CREATE EXTENSION IF NOT EXISTS vector`（幂等），使 Compose / CI / 裸机迁移都可靠。
