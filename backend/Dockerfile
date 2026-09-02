# StockMind V1 后端镜像（api / worker / beat / mock-supplier 共用）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# 先装依赖以利用构建缓存：依赖层只依赖 pyproject.toml + 锁文件，代码变更不会触发重装。
# PIP_INDEX_URL 默认使用清华镜像（容器内大文件下载更稳定），可通过 --build-arg 覆盖；
# --timeout/--retries 缓解间歇性网络超时（torch 等大 wheel 下载）。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG TORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY pyproject.toml README.md requirements.lock requirements-rag.lock ./
# 1) CPU-only PyTorch：StockMind 仅使用 CPU Embedding，不安装 CUDA 运行时。
#    官方 CPU wheel 源见 https://pytorch.org/get-started/locally/ （CPU 选项）。
#    固定 2.6.0+cpu（Python 3.11 官方 CPU wheel 最新可用版本）。
RUN pip install --no-cache-dir --timeout 300 --retries 5 \
    --index-url "$PIP_INDEX_URL" --extra-index-url "$TORCH_CPU_INDEX_URL" \
    "torch==2.6.0+cpu"
# 2) 其余依赖：--constraint requirements-rag.lock 使 base+rag 全部依赖与锁文件精确版本一致
#    （可重复构建）；lock 内含 torch==2.6.0+cpu，配合官方 CPU 源避免 pip 从通用源
#    重解析出 CUDA 版 torch（会带来 nvidia-* 依赖）。
RUN pip install --no-cache-dir --timeout 300 --retries 5 \
    -i "$PIP_INDEX_URL" --extra-index-url "$TORCH_CPU_INDEX_URL" \
    --constraint requirements-rag.lock ".[rag]"

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000 8100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
