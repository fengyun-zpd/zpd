# StockMind - 可审计的智能仓储补货 Agent

> 面向中小制造与批发仓库的可审计补货 Agent：Agent 负责理解与编排，确定性领域服务负责
> 计算与状态，授权人员负责高风险副作用。全部数据为固定随机种子生成的合成演示数据。

> **V1 冻结基线（2026-09-08）**：V1 功能边界、权限边界、状态机、数据模型和演示入口冻结。后续仅接受安全、数据正确性、构建阻塞和文档勘误修复；新增业务能力统一进入 V1.1 或更高版本，并须新增 ADR、测试和评测证据。

## 1. 快速开始（V1 本机 Docker Compose 闭环）

前置：Docker + Docker Compose。

```bash
cp .env.example .env          # 按需填写 LLM/观测凭证（可留空）
docker compose up -d --build
```

启动后：

| 入口 | 地址 |
|---|---|
| Web 工作区 | http://localhost:3000 |
| API 文档 | http://localhost:8000/docs |
| 模拟供应商状态 | http://localhost:8100/fault-modes |

### 1.1 面试演示（Windows）

在 Compose 启动后执行以下命令，可复验运行状态并运行权限、状态机和采购闭环核心测试；脚本不会清空数据库：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demo_interview.ps1
```

演示顺序建议为：补货助手发起计划 -> 审批箱批准/排除 -> 工作台核对“可采购/无需采购” -> 采购单按供应商创建 -> 以 `eve` 下达并登记到货 -> 关闭采购单；再切换模拟供应商超时，展示 `order_unknown` 只能查询恢复。演示完成后可在 API 文档查看请求与响应，审计记录保留实际操作者。

只检查正在运行的 Compose 而不重复本地测试时，可追加 `-SkipTests`。

需要隔离端口和数据卷演示完整采购状态机时，使用隔离 Compose（不影响主环境）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demo_interview.ps1 -Isolated
```

需要每次从干净合成数据开始时追加 `-Reset`；它只销毁隔离项目的卷：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demo_interview.ps1 -Isolated -Reset
```

隔离演示入口为 `http://127.0.0.1:13000`，API 文档为 `http://127.0.0.1:18000/docs`。演示结束后清理隔离卷：

```powershell
docker compose -f docker-compose.iso.yml -p stockmind-interview down -v
```

首次启动会在空数据库中初始化合成种子；API 重启不会清空已有业务数据。明确重置数据（一键销毁并重建合成种子）时：

```bash
docker compose exec api python -m app.seed.seed --reset
```

停止/清理：

```bash
docker compose down
docker compose down -v   # 连同数据卷一起清除
```

## 2. 使用 LLM 与 Embedding（可选）

- 补货助手对话：在 `.env` 填写 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。
  留空时进入 **OFFLINE 演示模式**（确定性意图/参数提取，不调用外部模型，UI 会标注 offline）。
- RAG 向量检索：`EMBEDDING_MODEL_NAME` 默认为 `paraphrase-multilingual-MiniLM-L12-v2`，
  首次运行联网下载（api 容器已挂载 `huggingface-cache` 缓存卷，重建不重复下载）；
  生成向量索引：`docker compose exec api python -m app.seed.seed --with-embeddings`；
  模型不可用时检索退化为关键词路径并记录告警。
- RAG 知识资料导入：切换到 `admin` 用户，打开“规则知识库”，粘贴经确认的 Markdown/TXT
  资料并提交。系统会按内容摘要幂等去重、分块建立关键词索引，并在 Embedding 可用时建立
  向量索引；普通用户只能检索和查看。导入资料仅作为可引用证据，不会直接修改安全库存、预测
  或补货公式；如要改变计算规则，必须走受控的结构化规则变更流程。

## 2.2 Langfuse 观测（可选，默认 no-op）

- 在 `.env` 填写 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`（`LANGFUSE_HOST` 默认
  `https://cloud.langfuse.com`）即启用：记录对话 trace、LLM generation、工具调用、
  RAG 检索、领域计算与错误，关联 request/trace/thread/plan id，记录模型名/耗时/
  Token/状态，并对 API Key 与敏感文本脱敏。
- 未配置凭证时完全安全 no-op（不初始化客户端、无网络请求），项目照常运行；
  观测失败（网络/异常）绝不改变业务事务结果。
- 云端验证需要真实凭证；无凭证时报告状态为"代码已实现、本地 mock 已验证、云端未验证"。

## 2.1 容器化说明（2026-09-01 已实测）

- pgvector 扩展双保险启用：`db` 服务通过 `docker/db-init/01-vector.sql` 首次初始化启用；
  **Alembic 迁移 `3fd22d8de190` 的 `upgrade()` 内也执行 `CREATE EXTENSION IF NOT EXISTS vector`**
  （幂等），因此 Compose、CI 的 PostgreSQL service（不挂载 db-init）与裸机迁移三种场景都可靠。
- `api` 服务启动命令为 `alembic upgrade head && python -m app.seed.seed && uvicorn`；
  种子命令默认只在空库初始化，只有显式 `--reset` 才重建，保证 api 容器重启不会清空业务状态。
- 依赖可重复构建：双锁文件——`backend/requirements.lock`（base+dev，CI/本机测试）、
  `backend/requirements-rag.lock`（base+rag，Docker 镜像；torch 固定官方 CPU 源
  `2.6.0+cpu`、零 CUDA/nvidia 依赖）。Dockerfile 与 CI 以 `--constraint` 应用；
  生成方式见 `.dev/gen_lock.sh` / `.dev/gen_rag_lock.sh`（pip-tools，不强制 uv）。
- `docker-compose.yml` 的 `env_file: .env` 使用 `required: false`（Compose long syntax）：
  无 `.env` 时（如干净 CI checkout）`docker compose config/build` 不报错，服务以 OFFLINE/no-op 运行。
- 超时故障演示参数：`api` 的 `ORDER_TIMEOUT_SECONDS=3`、`mock-supplier` 的
  `MOCK_SUPPLIER_TIMEOUT_SLEEP=8`（mock 延迟大于适配器超时才能进入 `order_unknown`）。
- 故障模式是模拟供应商进程的运行时状态，重置种子不会清除；需要时通过管理接口切换回 `normal`。

## 3. 手动闭环演示路径

```text
补货助手（自然语言发起补货）
  -> Agent 澄清必要参数（仓库/SKU/窗口 7|14|30）
  -> 只读证据工具 + 受控"生成补货草稿"
  -> 计划进入待审批，会话 interrupt 暂停
  -> 审批箱逐条批准/排除（PLAN_STALE 校验）
  -> 采购单页按供应商创建采购单
  -> 下达（模拟供应商；支持超时进入 order_unknown 后查询恢复）
  -> 分批登记到货 -> 确认关闭
```

定时闭环：定时任务页"立即执行"，或等待 Celery Beat 按数据库配置触发；
执行记录页查看逐 SKU 的 success/blocked/failed 与去重告警。

## 4. 目录结构

```text
backend/        FastAPI + SQLAlchemy + Celery + LangGraph + pgvector
  app/
    api/         REST/SSE 路由、错误处理、请求上下文
    models/      六域 ORM 模型（库存/补货/采购/Agent/知识库/治理）
    services/    确定性计算（预测/供应商/补货/哈希）+ 领域服务（计划/采购/幂等/审计/锁）
    rag/         Embedding、jieba+FTS、RRF 融合、规则解析
    agent/       LangGraph 图、工具白名单、offline/LLM 双模式、interrupt/resume
    tasks/       Celery：定时扫描、未知订单恢复、workflow_resume 触发
    seed/        固定种子合成数据（可一键重建）
    mock_supplier/ 模拟供应商 API（故障模式由 admin 切换）
  alembic/       PostgreSQL 迁移（含部分唯一索引/检查约束/pgvector）
  tests/         unit / property / integration / agent / e2e
frontend/       React + TypeScript + Vite（助手/工作台/审批箱/采购单/定时任务/执行记录/规则知识库/库存与数据）
docker-compose.yml  V1 本机一键启动
```

## 5. 测试与静态检查

```bash
# 后端非 E2E 测试（需 PostgreSQL 14 + pgvector + Redis；无数据库时数据库用例自动跳过并说明原因）
cd backend
pip install -e ".[dev]" --constraint requirements.lock
pytest -q -m "not e2e"        # 单元/性质/集成测试（不含浏览器）

# 隔离环境 E2E 测试（不重置主项目数据，见下方"6.2 隔离 E2E 环境"）
bash scripts/prepare-e2e-isolated.sh
cd backend && E2E_BASE_URL=http://localhost:13000 pytest -q -m e2e

# 静态检查
ruff check app tests
ruff format --check app tests
mypy app

# 前端
cd frontend
npm install && npm run build

# 黄金集评测（可复现；RAG/对话指标走运行中的 API；MODE=offline|llm）
cd backend && bash evaluation/run_eval.sh http://127.0.0.1:8000 offline
cd backend && bash evaluation/run_eval.sh http://127.0.0.1:8000 llm   # 需 .env 配置 LLM_API_KEY
# 报告输出到 backend/evaluation/reports/eval_report_{offline,llm}.json
```

## 5.1 主项目与隔离 E2E 环境

| 环境 | compose 文件 | 端口 | 用途 | 数据 |
|---|---|---|---|---|
| 主项目 | `docker-compose.yml`（project `stockmind`） | 前端 3000 / API 8000 / mock 8100 / PG 5432 / Redis 6379 | 日常演示与业务闭环 | 业务库 `stockmind`，**绝不自动 reset** |
| 隔离 E2E | `docker-compose.iso.yml`（project `stockmind-isolate`） | 前端 13000 / API 18000 / mock 18100 / PG 15432 / Redis 16379 | 浏览器 E2E / 全新部署验证（可安全重置种子） | 独立卷 `stockmind-isolate_*`，与主项目互不影响 |

隔离 E2E 使用方式：

```bash
# 启动隔离环境并准备干净种子（可交付脚本；从脚本位置推导仓库根，无固定路径）
bash scripts/prepare-e2e-isolated.sh          # 构建 + 启动 + 重置隔离库 + 等 Beat 计划 + 恢复故障模式
bash scripts/prepare-e2e-isolated.sh --no-build  # 跳过构建（镜像已存在时更快）

# 运行完整 E2E（只指向隔离前端，绝不触碰主项目 3000/8000 数据）
cd backend && E2E_BASE_URL=http://localhost:13000 pytest -q -m e2e

# 停止隔离环境（保留主项目运行）
docker compose -f docker-compose.iso.yml down
```

脚本说明：`scripts/prepare-e2e-isolated.sh` 只操作 `docker-compose.iso.yml` 的隔离容器/隔离库/隔离端口；
重置种子与恢复故障模式全部指向 `18000/18100/13000`，不会影响主项目。健康检查与"等待 Beat 计划"使用
带超时与明确报错的轮询（不依赖固定长时间 sleep）。

**验证结果（2026-09-02 发布基线实测，本机 Windows PowerShell + WSL）**：

- 后端非 E2E 测试（unit/property/integration/agent，不含浏览器）：**136 passed, 8 deselected**
  （V1 收口后：原 129 项 + 缺参澄清文案/预算不参与/全零计划建单拒绝/重复建单拒绝等 7 项新增）。
- 隔离环境 E2E 测试（Playwright，独立 compose `docker-compose.iso.yml`，前端 :13000）：**8 passed**
  （闭环 助手→审批→建单→下达→分批收货→关闭、助手缺参/防重/模式徽标、数据页库存/供应商/告警/工作台详情）。
- 安全不变量专项检查：越权操作、重复有效建议、重复采购、重复入库、`order_unknown` 盲目重试、非法状态迁移、幂等键异载荷副作用 **7 项全部为 0**。
- 静态检查：Ruff、格式检查、Mypy 全部通过。
- HTTP 冒烟：真实服务上走通 对话 SSE → 草稿 → 审批 → 按供应商拆单 → 模拟下单（真实 HTTP）→ 分批收货 → 关闭 → 立即执行定时扫描；故障注入验证 明确失败→po_created、超时→order_unknown→查询→ordered、越权切换故障模式→403。
- **真实 LLM**：使用 `.env` 配置（DeepSeek，Key 不输出）验证通过——意图/参数提取由真实模型完成、草稿生成并 interrupt；LLM 不可用时自动回退 OFFLINE 演示模式。
- **checkpoint 连接自愈（发布修复）**：运行中 api 执行 `seed --reset`（DROP SCHEMA）后，LangGraph Postgres checkpoint 长连接被终止曾导致后续对话全部失败；现已改为连接健康探测 + 失效自动重建 graph，reset 后无需重启容器即可继续对话（隔离环境实测）。
- 端到端浏览器（Playwright + Chromium，隔离环境 :13000 实测）：真实页面走通 助手→审批箱→采购单建单→下达→分批收货→关闭；运行前执行可交付脚本 `scripts/prepare-e2e-isolated.sh`（只重置隔离库，不触碰主项目；测试选中本轮新建 PO，不误操作 SEED-PO；无 Vite dev server 冒充容器 nginx）。
- **Docker Compose 容器化启动（已实测）**：Docker Hub 网络恢复后 6 个镜像全部拉取成功（hello-world、redis:7-alpine、pgvector/pgvector:pg16、python:3.11-slim、node:20-alpine、nginx:1.27-alpine）；`docker compose build` 成功；7 个服务全部 Up（db/redis healthy）；容器内迁移与种子连续执行幂等可重复；api 容器 restart 后可重复启动（alembic 幂等）；API/前端/模拟供应商端点实测 200。
- **镜像瘦身（发布基线已实测）**：后端镜像改用官方 CPU-only PyTorch（`torch==2.6.0+cpu`，官方 CPU wheel 源），**8.81GB → 2.21GB（-75%）**，容器内确认零 `nvidia-*` 包（原 15 个 CUDA 相关包已移除）、`torch.cuda.is_available()=False`；重建后容器内验证 Embedding（384 维）、混合检索 5 命中、真实 LLM 对话、容器 nginx e2e 全部正常。依赖锁定：`torch==2.6.0+cpu`、`sentence-transformers>=4.1,<5`、`langgraph>=0.3.30,<0.4`、`langgraph-checkpoint-postgres>=2.0.24,<2.1`、`langfuse>=2.53,<3`；默认依赖源为可信镜像源（清华 PyPI），可通过构建参数覆盖。
- **Embedding 向量路径（已实测）**：容器内安装 [rag] 依赖并下载模型（已挂载 huggingface-cache 缓存卷）；`seed --with-embeddings` 生成 16 条向量；pgvector 余弦召回实测命中（相似度 0.82）；关键词 FTS 与 RRF 混合检索实测命中；提示注入/伪造引用文档被过滤。
- **黄金集评测（双模式分表，已实测）**：`run_eval.sh` 输出 OFFLINE 与真实 LLM 两份报告（样本量、并发、机器、模型、时间戳如实记录）——参数字段准确率 0.857、必要澄清率 1.0、RAG Recall@5=0.8 / MRR=0.8 / 引用正确率 1.0、MAE=1.32 / WAPE=0.14；OFFLINE 任务完成率 0.667 / 工具调用正确率 0.929（P50 5.7ms）；真实 LLM 任务完成率 0.667 / 工具调用正确率 0.929（P50 1.68s、Token 总量 859=输入 738+输出 121）；成本未配置单价只报 Token。
- **Langfuse 观测（已实现，mock 已验证，云端未验证）**：无凭证安全 no-op；有凭证记录对话/LLM generation/工具/RAG/领域计算与错误，关联 request/trace/thread/plan id，记录模型名/耗时/Token/状态，对 API Key 与敏感文本脱敏；观测失败不影响业务事务；本地 mock 单测 5 项通过。云端未验证（`.env` 未配置 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`）——唯一外部阻塞；填写后即可接入。
- **健康检查分层（已实现）**：`/health` 只表示进程存活并返回 Agent 能力标识；`/ready` 仅在 Agent 路由成功挂载时返回 ready，依赖缺失时稳定返回 503，避免部署探针误把降级服务当作完整业务。
- **隔离全新部署验证（已实测）**：独立 compose project（`docker-compose.iso.yml`）、独立卷（`stockmind-isolate_*`）、不冲突端口（18000/13000/18100/15432/16379）：全新镜像构建、pgvector 扩展初始化（0.8.6）、`alembic upgrade head` 幂等、种子重复执行幂等、api 重启恢复、Redis/PG/Worker/Beat 连通、OFFLINE 对话、Embedding 16 条向量、HTTP 冒烟闭环（5 张 PO 下达→收货→关闭）、容器 nginx 前端 Playwright E2E、停止后重启全部恢复。
- CI（无密钥可运行，本轮修复后已本地等价验证）：后端检查（Ruff/格式/Mypy/测试）、
  **迁移内自动启用 pgvector 扩展**、迁移与种子幂等（干净库 alembic→seed×2→alembic→pytest 顺序验证）、
  前端构建、容器构建检查（无 `.env` 时 `env_file: required:false` 可 config/build）、
  密钥泄露扫描（只报文件名，不输出匹配内容），全部在无 LLM/Langfuse 凭证环境执行。
  注：workflow 文件已按上述修复更新，本轮以本地等价流程验证通过；
  GitHub Actions 云端成功运行需要 push 后由 Actions 执行（仓库当前未提交，无云端运行记录）。
- 收口修复：①`node_classify` 会话块外使用 session 的连接泄漏；②LLM 意图分类提示词与分类名展开；③Docker Compose 补 db 初始化 pgvector 扩展脚本；④seed 默认只在空库初始化、显式 `--reset` 才重建（保证 api 可重复启动不清数据）；⑤Dockerfile pip 镜像源/超时与层缓存顺序；⑥pgvector 向量检索改用 ORM（`Vector.cosine_distance`）修复 raw SQL 绑定失败；⑦e2e 采购单下拉按可读状态文本选中（容器环境残留历史 PO 时不再用 index 定位）+ 前置脚本检查种子状态并重置故障模式；⑧后端镜像改用官方 CPU-only PyTorch（消除 CUDA 运行时依赖）；⑨Langfuse 可选观测组件实现；⑩Alembic 迁移内 `CREATE EXTENSION IF NOT EXISTS vector`（CI/裸机不再依赖 db-init）；⑪compose `env_file` 改 `required: false`（干净 CI 无 `.env` 可 config/build）；⑫`backend/requirements.lock` 锁文件（pip-tools，Dockerfile/CI 以 `--constraint` 应用）；⑬评测脚本 OFFLINE 模式强制禁用 LLM（避免误用真实模型）与 CI 密钥扫描不泄露匹配内容；⑭多行采购单收满一行不得提前 received（仅全部明细收齐才 received，需求 6.3）；⑮建单过滤 `order_qty=0` 明细（净需求 0 不建 0 数量行）；⑯新增"库存与数据"只读页（库存/供应商/告警）与补货工作台计划详情展开（SKU 明细/阻断原因/下一步）；⑰e2e 前置脚本改 `seed --reset` 显式重建（消除前次运行残留导致的 PLAN_STALE）；⑱补货助手缺参澄清改为自然业务语言并明确引导仓库/SKU 或商品分类/规划周期（7/14/30 天），回复不再暴露 `warehouse_id` 等内部字段名，missing 白名单化；⑲预算/成本输入识别后明确说明“预算暂不参与 V1 计算”且绝不当必填参数（V2 金额阈值审批前不改变计算）；⑳补货助手新增可点击示例请求按钮；㉑补货工作台对建议数量为 0 的有效明细显示“无需采购”，计划行显示“可采购 X 条 / 无需采购 Y 条”，无可采购明细的计划提示不引导建单；㉒建单接口对全零计划返回稳定校验错误（422“该计划无需建单”，无副作用）并对同一计划重复建单返回可读 409（原依赖 DB 唯一约束 500），`/plans` 返回 `purchasable_count/zero_qty_count/has_po` 供页面过滤（只列正数量且未建单计划，全零/已建单计划给出提示）；㉓审批箱空状态说明待审批计划来源与已批准计划去向；㉔定时任务页面按角色提前禁用“立即执行”（操作员只读查看并说明仅管理员可用），后端 admin-only 校验保留（操作员直连 403 实测）；㉕执行记录默认只显示最近 20 条并明确标注；㉖测试 conftest 强制测试环境隔离（`POSTGRES_DSN`/`LLM_API_KEY=""`/内存 checkpoint），杜绝容器 `.env` 导致测试误连主库或真实调用 LLM；㉗工作台/采购单/执行记录等页面文案与状态中文统一核对（内部状态码仅作调试 title 展示）。

## 6. 关键设计与安全不变量

- **计算归属**：规则校验 → 预测 → 选供应商 → 计算补货量，全部确定性服务；
  供应商必须先于补货量选定；LLM 不参与数值计算。
- **唯一写工具**：LLM 只有"生成补货草稿"；审批/建单/下单/收货/关闭/取消/规则修改/
  故障切换只允许对应角色或后台任务。知识资料导入仅允许 `admin` 通过页面/API 操作，且是
  证据导入，不改变结构化计算规则。
- **数据库权威**：计划/审批/采购状态以 PostgreSQL 为准；checkpoint 只恢复会话。
- **决策新鲜度**：审批与建单前重算 `decision_input_hash`，变化返回 409 `PLAN_STALE`。
- **防重**：活动建议部分唯一索引、采购单明细 `plan_line_id` 唯一、`receipt_event_id`
  全局唯一、执行/执行明细行级幂等、告警去重部分唯一索引。
- **外部副作用**：下单先持久化 attempt + `ordering` 再外呼；超时/歧义进入
  `order_unknown`，只能查询供应商恢复，禁止盲目重试或换键重下。
- **安全不变量**（评测必须为 0）：越权操作、重复有效建议、重复采购、重复入库、
  未知状态盲目重试、非法状态迁移、幂等键异载荷副作用。

## 7. 诚实边界

- 全部数据为固定随机种子生成的合成数据；不声称接入真实 WMS/ERP/供应商，
  不声称产生真实经营收益。
- V1 只实现需求规格说明书 V4.5 的 V1 范围；V1.1/V2 能力标注为规划中/未实现。
- 配置 LLM Key 时对话使用真实模型（已实测 DeepSeek）；无 Key 或调用失败时自动回退
  OFFLINE 演示模式（UI 标注 offline）。无 Embedding 模型时向量召回不可用（如实告警），
  关键词检索路径可正常降级。
- 未实测指标不得外推为真实业务收益。

## 8. 文档索引

- [V1 使用说明书（小白版）](StockMind使用说明书.md)
- [需求规格说明书 V4.6](StockMind需求规格说明书.md)
- [架构设计文档 V4.6](StockMind架构设计文档.md)
- [ADR 001 修订版 1.7](StockMind架构决策记录ADR001.md)
- [工作区 Agent 宪法 v1.5](AGENTS.md)

### 8.1 模块化演进区

新项目远程仓库：<https://github.com/fengyun-zpd/dianshang-shouhou>（当前公开仓库为空，初始化前不宣称已有远程代码）。

`docs/` 将 V1 已验证的确定性补货能力拆成可独立迭代的平台模块，并规划电商售后多智能体工单系统的 V2/V3 演进。入口见 [模块化演进文档](docs/README.md)，其他 Agent 的执行提示词见 [面试叙事与提示词](docs/12_interview/README.md)。

开始分工时直接使用根目录的 [新项目 Agent 启动说明](AGENT_START_HERE.md)。

模块文档中的能力分为“V1 已实现”“V2 规划”“V3 规划”三类；规划内容必须先有 ADR、代码、测试和评测证据，才可升级为已实现。

## 9. 修订记录

- 2026-09-08：V1 冻结基线生效。冻结现有功能、权限、状态机、数据模型和演示流程；后续新增能力不得直接进入 V1，安全/正确性/构建阻塞修复仍可进入冻结基线。

- 2026-09-07：会话身份收紧——创建会话以 `X-Actor-Id` 为唯一事实源，拒绝请求体身份不一致；读取/发送消息校验 `thread_id` 归属，补上跨演示用户会话访问边界，并新增集成回归测试。

- 2026-09-07：面试级收紧——采购领域服务与 REST API 角色矩阵统一，纯 `admin` 可演示下单、未知订单查询、收货、关闭和取消；下单审计记录真实操作者；新增 `/health` 能力标识与 `/ready` 就绪检查；补充纯管理员集成闭环与健康检查单测。

- 2026-09-04：登记新项目远程 `fengyun-zpd/dianshang-shouhou`；新增 `docs/` 模块化演进文档、ADR 和可直接分配给其他 Agent 的提示词。V1 实现与验收边界不变，售后多 Agent、MCP、Graphiti/Neo4j、微调和 Mule Agent Bridge 标为规划中。

- 2026-09-02：V1 知识库可用性修复——新增管理员 Markdown/TXT 证据资料导入、内容摘要幂等去重、分块关键词/可选向量索引、导入审计和导入后检索验证；普通用户保持只读，资料不会直接修改结构化补货规则；同步使用说明、需求、架构、ADR 与宪法至 V4.4 / ADR 1.4 / v1.4。
- 2026-09-02：V1 收口修复与验收（第二波）——补货助手缺参澄清业务语言+预算不参与说明+示例按钮；工作台“无需采购/可采购统计”；建单全零计划稳定 422 与重复建单 409 防护、`/plans` 增加可采购/无需采购/已建单统计；审批箱空状态引导；定时任务前端按角色禁用按钮（后端 403 保留）；执行记录默认最近 20 条；测试环境隔离加固。后端 136 passed（新增 7 项）、隔离 E2E 8 passed、真实浏览器 localhost:3000 验收 31 项全过（详情见 `.dev/accept_out.txt`）；7 项安全不变量不变量保持不变（领域规则/状态机/权限边界未改动）。
- 2026-09-01：V1 发布收口第一轮功能优化——依赖可复现升级为双锁文件（`requirements-rag.lock` 锁定 base+rag、torch 固定 CPU 版 2.6.0+cpu、零 CUDA 依赖）；补货助手与审批箱错误可见性/恢复体验增强（缺参字段、阻断原因+下一步、PLAN_STALE 变化字段、order_unknown 只查询、幂等键区分、对话/LLM/RAG 状态区分）；新增 6 项单元测试与 3 个 Playwright 场景；全套测试 122 项、7 项安全不变量全为 0。
- 2026-09-01：V1 发布候选审计与 CI 收口——修复 CI 真实阻塞（迁移内启用 pgvector 扩展、compose `env_file: required:false`、密钥扫描不泄露、CI 步骤顺序、`POSTGRES_DSN` 一致性）；新增 `requirements.lock` 依赖锁文件（Dockerfile/CI 以 `--constraint` 应用，torch 仍由官方 CPU 源固定 2.6.0+cpu）；评测脚本 OFFLINE 模式强制禁用 LLM（此前误用真实模型）；本地等价验证 CI backend 全流程与容器业务语义复验通过。
- 2026-09-01：V1 发布基线收口——后端镜像 CPU-only PyTorch 瘦身（8.81GB → 2.21GB）；Langfuse 可选观测实现（mock 已验证、云端未验证）；黄金集评测 OFFLINE/真实 LLM 双模式分表；隔离全新部署验证通过；CI 增加容器构建检查与密钥扫描；全套测试 116 项、7 项安全不变量全为 0；四份基础文档同步升级 V4.3 / ADR 1.3 / AGENTS v1.3。
- 2026-09-01：V1 收口——真实 LLM（DeepSeek）对话验证通过；安全不变量 7 项全为 0；修复 Agent 会话连接泄漏；修复 LLM 意图分类提示词与分类名展开；Docker Compose 容器启动因本机无法连接 Docker Hub registry 未实测；验证结果章节按本次实测更新。
- 2026-08-31：V1 本机闭环实现完成（迁移/种子/测试/冒烟/浏览器验证）；四份基础文档同步至 V4.2 / ADR 1.2。

## License

MIT，见 [LICENSE](LICENSE)。
