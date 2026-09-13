#!/usr/bin/env bash
# StockMind V1 可复现黄金集评测入口（发布基线版）
# 用法：./run_eval.sh [API_BASE_URL] [MODE]
#   MODE=offline （默认）：对话指标走离线确定性模式（不调用 LLM）
#   MODE=llm     ：对话指标走真实 LLM（需 .env 配置 LLM_API_KEY；每轮真实调用）
# 确定性指标（参数/RAG/预测）两模式一致；P50/P95、Token、成本只在实际调用处采样。
# 报告样本量、模式、模型、机器与时间戳。
set -uo pipefail
# 加载仓库根目录 .env（含 LLM_API_KEY 等；仅本次进程使用，不输出任何值）
ENV_FILE="$(cd /mnt/d/workplace/PyCharmMiscProject/仓储 && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
export POSTGRES_DSN='postgresql+psycopg://stockmind:stockmind@127.0.0.1:5432/stockmind'
export EMBEDDING_ENABLED='true'
export CHECKPOINTER_BACKEND='memory'
cd /mnt/d/workplace/PyCharmMiscProject/仓储/backend
/opt/stockmind-venv/bin/python - "$@" <<'PYEOF'
import json
import os
import platform
import sys
import time
import urllib.parse

import httpx

API_BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
MODE = sys.argv[2] if len(sys.argv) > 2 else "offline"

# MODE 只允许 offline / llm：其它取值立即失败，避免悄悄跑成某种混合模式。
if MODE not in ("offline", "llm"):
    print(f"ERROR: MODE 只能是 offline 或 llm，收到 {MODE!r}", file=sys.stderr)
    sys.exit(2)

if MODE == "offline":
    # 强制离线：清空外部 LLM 凭证并显式设置 LLM_MODE=offline。
    # （.env 会被本脚本 source，可能含 LLM_API_KEY；不清理会让 "OFFLINE 确定性" 名不副实。）
    os.environ["LLM_MODE"] = "offline"
    os.environ["LLM_API_KEY"] = ""
    os.environ["LANGFUSE_PUBLIC_KEY"] = ""
    os.environ["LANGFUSE_SECRET_KEY"] = ""
else:
    # 评测真实模型路径：显式声明 llm 模式；缺少完整配置时由 Agent 记录
    # LLM_NOT_CONFIGURED 并降级，绝不伪造"真实调用成功"。
    os.environ["LLM_MODE"] = "llm"

# 配置完整性：llm 模式下若缺任一配置，本次结果不代表真实模型
LLM_CONFIGURED = bool(
    (os.environ.get("LLM_API_KEY", "") or "").strip()
    and (os.environ.get("LLM_BASE_URL", "") or "").strip()
    and (os.environ.get("LLM_MODEL", "") or "").strip()
)
API = f"{API_BASE}/api/v1"
EVAL_DIR = "evaluation"
with open(os.path.join(EVAL_DIR, "golden_set.json"), encoding="utf-8") as f:
    GOLDEN = json.load(f)


def percentile(values: list[float], quantile: float) -> float | None:
    """使用 nearest-rank 计算小样本 P50/P95，避免 n=3 时索引落到 P33。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int((quantile * len(ordered)) + 0.999999)))
    return ordered[rank - 1]

# ---------- 运行上下文 ----------
report = {
    "schema_version": GOLDEN["schema_version"],
    "api": API_BASE,
    "mode": MODE,
    "llm_mode": os.environ.get("LLM_MODE", ""),
    "llm_configured": LLM_CONFIGURED,
    "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "host": platform.node(),
    "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
    "python": platform.python_version(),
    "concurrency": 1,
    "model": (os.environ.get("LLM_MODEL", "") or ""),
    "metrics": {},
}
corpus_fingerprint = "seed-20260831"

# ---------- 1) 参数字段准确率（确定性，可复现） ----------
from app.agent import offline
from app.db import get_session_factory

param_total = 0
param_correct = 0
clarify_cases = 0
clarify_correct = 0
with get_session_factory()() as session:
    for case in GOLDEN["parameter_extraction"]:
        parsed = offline.parse_params(case["input"], session)
        exp = case["expected"]
        if exp.get("intent"):
            ok = parsed.intent == exp["intent"]
        elif exp.get("clarify"):
            ok = bool(parsed.missing)
            clarify_cases += 1
            clarify_correct += 1 if ok else 0
        else:
            fields = ["warehouse_id", "requested_window"]
            ok = True
            for fld in fields:
                if exp.get(fld) is not None:
                    ok = ok and parsed.params.get(fld) == exp[fld]
            if "products_any_of" in exp:
                got = set(parsed.params.get("products", []))
                ok = ok and bool(got & set(exp["products_any_of"]))
        param_total += 1
        param_correct += 1 if ok else 0
report["metrics"]["param_field_accuracy"] = (
    round(param_correct / param_total, 4) if param_total else None
)
report["metrics"]["necessary_clarify_rate"] = (
    round(clarify_correct / clarify_cases, 4) if clarify_cases else None
)

# ---------- 2) RAG Recall@5 / MRR / 引用正确率（API，容器内有向量） ----------
def rag_eval(query):
    url = f"{API}/knowledge/search?q={urllib.parse.quote(query)}&top_k=5"
    r = httpx.get(url, headers={"X-Actor-Id": "alice"}, timeout=60)
    r.raise_for_status()
    return [h["document_id"] for h in r.json()["data"]]

recall_hits = 0
mrr_sum = 0.0
cite_ok = 0
cite_total = 0
rag_latencies: list[float] = []
for q in GOLDEN["rag_queries"]:
    t0 = time.perf_counter()
    docs = rag_eval(q["query"])
    rag_latencies.append(time.perf_counter() - t0)
    relevant = set(q.get("relevant_docs", []))
    reject = set(q.get("reject_docs", []))
    hit = bool(docs) and bool(relevant & set(docs))
    recall_hits += 1 if hit else 0
    for i, d in enumerate(docs, 1):
        if d in relevant:
            mrr_sum += 1.0 / i
            break
    cite_ok_this = (not reject & set(docs)) and (not relevant or hit)
    cite_ok += 1 if cite_ok_this else 0
    cite_total += 1
n_rag = len(GOLDEN["rag_queries"])
report["metrics"]["rag_recall_at_5"] = round(recall_hits / n_rag, 4)
report["metrics"]["rag_mrr"] = round(mrr_sum / n_rag, 4)
report["metrics"]["rag_citation_correctness"] = round(cite_ok / cite_total, 4)
report["metrics"]["rag_sample_size"] = n_rag
if rag_latencies:
    report["metrics"]["rag_p50_latency_s"] = round(percentile(rag_latencies, 0.5), 4)
    report["metrics"]["rag_p95_latency_s"] = round(percentile(rag_latencies, 0.95), 4)

# ---------- 3) 预测 MAE/WAPE（种子需求序列，确定性） ----------
from sqlalchemy import select
from app.models.inventory import Product
from app.services.forecast import forecast_daily_demand, _wma_forecast, _ses_forecast
from app.services.inventory_service import business_date, get_demand_series
from decimal import Decimal

with get_session_factory()() as session:
    product = session.scalar(select(Product).where(Product.id == "SKU-E01"))
    bdate = business_date("Asia/Shanghai")
    series = get_demand_series(session, "WH-E", "SKU-E01", upto=bdate)
    fc = forecast_daily_demand(series)
    consec = [d for d in series if d.source_complete]
    if len(consec) >= 56:
        errs = []
        abs_actual = Decimal(0)
        for i in range(len(consec) - 28, len(consec)):
            train = [d.demand for d in consec[i - 28:i]]
            actual = Decimal(consec[i].demand)
            f = _wma_forecast(train) if fc.algorithm == "wma_28_v1" else _ses_forecast(train)
            errs.append(abs(f - actual))
            abs_actual += actual
        mae = sum(errs) / Decimal(len(errs))
        wape = (sum(errs) / abs_actual) if abs_actual > 0 else None
        report["metrics"]["forecast_mae"] = float(round(mae, 4))
        report["metrics"]["forecast_wape"] = float(round(wape, 4)) if wape is not None else None
        report["metrics"]["forecast_algorithm"] = fc.algorithm
        report["metrics"]["forecast_backtest_days"] = 28
    else:
        report["metrics"]["forecast_mae"] = None
        report["metrics"]["forecast_wape"] = None

# ---------- 4) 对话任务/工具指标（offline 或真实 LLM，按 MODE） ----------
from app.agent.graph import run_turn
from app.observability import reset_token_counts, token_counts

# 刷新 settings 缓存，确保 MODE 对应的 LLM 开关生效
# （offline：上方已清空 LLM_API_KEY -> _llm_enabled()=False）
if MODE == "llm":
    os.environ["LLM_API_KEY"] = os.environ.get("LLM_API_KEY", "")
from app.config import get_settings

get_settings.cache_clear()

reset_token_counts()
task_total = 0
task_ok = 0
tool_ok_calls = 0
tool_total_calls = 0
turn_latencies: list[float] = []
degraded_turns = 0            # 真实降级次数（不含主动 offline 选择）
degradation_reasons: dict = {}
for case in GOLDEN["dialog_cases"]:
    thread = f"eval-{int(time.time()*1000)}-{task_total}"
    t0 = time.perf_counter()
    state, interrupted = run_turn(thread, "alice", case["input"])
    turn_latencies.append(time.perf_counter() - t0)
    exp = case["expect"]
    if exp.get("interrupted"):
        ok = interrupted
    elif exp.get("clarify"):
        ok = bool(state.get("missing_params"))
    elif exp.get("intent"):
        ok = state.get("intent") == exp["intent"]
    else:
        ok = False
    task_total += 1
    task_ok += 1 if ok else 0
    calls = state.get("tool_calls", [])
    tool_total_calls += len(calls)
    tool_ok_calls += sum(1 for c in calls if c.get("ok"))
    # 降级原因必须显式统计（不允许静默降级）；LLM_MODE_OFFLINE 是主动选择，不计入降级
    reason = state.get("degradation_reason")
    if reason:
        degradation_reasons[reason] = degradation_reasons.get(reason, 0) + 1
        if reason != "LLM_MODE_OFFLINE":
            degraded_turns += 1
token_input, token_output, token_total = token_counts() if MODE == "llm" else (0, 0, 0)
report["metrics"]["dialog_task_completion_rate"] = round(task_ok / task_total, 4)
report["metrics"]["tool_call_correctness"] = (
    round(tool_ok_calls / tool_total_calls, 4) if tool_total_calls else None
)
report["metrics"]["dialog_sample_size"] = task_total
report["metrics"]["dialog_mode"] = MODE
report["metrics"]["dialog_offline_mode"] = MODE != "llm"
report["metrics"]["degradation_count"] = degraded_turns
report["metrics"]["degradation_rate"] = (
    round(degraded_turns / task_total, 4) if task_total else None
)
report["metrics"]["degradation_reasons"] = degradation_reasons
if turn_latencies:
    report["metrics"]["dialog_p50_latency_s"] = round(percentile(turn_latencies, 0.5), 4)
    report["metrics"]["dialog_p95_latency_s"] = round(percentile(turn_latencies, 0.95), 4)
# Token 与成本：仅在真实 LLM 模式且配置单价时报告数值（未配置单价 -> null，绝不写 0）
report["metrics"]["token_input_total"] = token_input if MODE == "llm" else None
report["metrics"]["token_output_total"] = token_output if MODE == "llm" else None
report["metrics"]["token_total"] = token_total if MODE == "llm" else None
price_input = (os.environ.get("LLM_PRICE_PER_1K_INPUT", "") or "").strip()
price_output = (os.environ.get("LLM_PRICE_PER_1K_OUTPUT", "") or "").strip()
report["metrics"]["cost_total_usd"] = None
report["metrics"]["cost_per_task_usd"] = None
if MODE != "llm":
    # OFFLINE 强制禁用 LLM：不产生 Token，也不得产生任何 LLM 成本
    report["metrics"]["cost_price_source"] = "OFFLINE 模式不调用外部模型，不产生 LLM 成本"
    report["metrics"]["cost_price_assumed"] = False
elif not (price_input and price_output):
    report["metrics"]["cost_price_source"] = "未配置 LLM_PRICE_PER_1K_INPUT/OUTPUT，仅报告 Token"
    report["metrics"]["cost_price_assumed"] = False
elif not token_total:
    report["metrics"]["cost_price_source"] = "已配置单价但未采集到 Token，成本为 null"
    report["metrics"]["cost_price_assumed"] = True
else:
    try:
        total_cost = (token_input / 1000) * float(price_input) + (token_output / 1000) * float(price_output)
        report["metrics"]["cost_total_usd"] = round(total_cost, 8)
        report["metrics"]["cost_per_task_usd"] = (
            round(total_cost / task_total, 8) if task_total else None
        )
        report["metrics"]["cost_price_source"] = "LLM_PRICE_PER_1K_INPUT/OUTPUT（环境变量配置的假设单价）"
        report["metrics"]["cost_price_assumed"] = True
    except ValueError:
        report["metrics"]["cost_price_source"] = "单价解析失败，成本为 null（仅报告 Token）"
        report["metrics"]["cost_price_assumed"] = True

# ---------- 5) 观测状态 ----------
report["metrics"]["langfuse_trace"] = (
    "已实现；本地 mock 单测通过；云端未验证（未配置 LANGFUSE_PUBLIC_KEY/SECRET_KEY）"
)
report["corpus_fingerprint"] = corpus_fingerprint
report["notes"] = [
    "合成种子数据；结果不代表真实经营收益。",
    "延迟为单线程小样本（样本量见 *_sample_size），不构成容量结论，也不代表生产 QPS。",
    "真实 LLM 模式结果受模型/网络影响，非确定性；offline 模式确定性可复现。",
    "成本按配置单价（LLM_PRICE_PER_1K_INPUT/OUTPUT）**假设单价**估算，不是供应商真实账单；"
    "生产环境必须用真实账单校准；未配置单价时成本字段为 null（不写 0）。",
    "OFFLINE 模式强制不调用外部模型：不产生 Token 与 LLM 成本；"
    "degradation_reasons 中的 LLM_MODE_OFFLINE 是主动选择，不计入 degradation_count。",
]
if MODE == "llm" and not LLM_CONFIGURED:
    report["notes"].append(
        "本次 MODE=llm 但 LLM 配置不完整：对话全部降级为 OFFLINE"
        "（degradation_reasons 中可见 LLM_NOT_CONFIGURED），**不代表真实模型结果**。"
    )
if MODE == "offline":
    report["notes"].append("本次 MODE=offline 已显式设置 LLM_MODE=offline，不产生任何外部模型调用。")

os.makedirs(os.path.join(EVAL_DIR, "reports"), exist_ok=True)
out_path = os.path.join(EVAL_DIR, "reports", f"eval_report_{MODE}.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(json.dumps(report, ensure_ascii=False, indent=2))
print("报告已写入:", out_path)
PYEOF
echo "EVAL_EXIT=$?"
