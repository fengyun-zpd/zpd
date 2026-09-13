# StockMind V1 黄金集评测

可复现评测入口：`backend/evaluation/run_eval.sh`（确定性指标在本机 venv 计算；
RAG 与对话指标通过 API 执行——容器内已含向量与真实/离线对话路径）。

## 运行方式

```bash
# OFFLINE 确定性模式（默认；对话指标不调用 LLM，确定性可复现）
bash backend/evaluation/run_eval.sh http://127.0.0.1:8000 offline

# 真实 LLM 模式（需 .env 配置 LLM_API_KEY；每轮真实调用 DeepSeek）
bash backend/evaluation/run_eval.sh http://127.0.0.1:8000 llm
```

报告输出：`backend/evaluation/reports/eval_report_{offline|llm}.json`。

## 模式一致性（必须遵守）

- `MODE` **只接受 `offline` 或 `llm`**，其它取值立即以退出码 2 失败（不会悄悄跑成混合模式）。
- `MODE=offline`：脚本强制设置 `LLM_MODE=offline` 并清空 `LLM_API_KEY` / Langfuse 凭证，
  保证"OFFLINE 确定性"名副其实，且不产生任何外部模型调用与 Token。
- `MODE=llm`：脚本强制设置 `LLM_MODE=llm`。若 LLM 配置不完整，Agent 会记录
  `LLM_NOT_CONFIGURED` 并降级为 OFFLINE；报告 `llm_configured=false` 且 `notes` 明确写出
  "**不代表真实模型结果**"，**绝不伪造真实调用成功**。
- 报告字段 `mode` / `llm_mode` / `llm_configured` 三者共同界定本次运行的真实路径。
- `degradation_count` / `degradation_rate` 只统计**真实失败降级**；
  `LLM_MODE_OFFLINE`（主动选择）保留在 `degradation_reasons` 分布中但不计入降级次数。

## 指标分表

| 维度 | offline 模式 | llm 模式 |
|---|---|---|
| 参数字段准确率 | 确定性（offline.parse_params） | 同左（参数提取独立于 LLM） |
| 必要澄清率 | 确定性 | 同左 |
| RAG Recall@5 / MRR / 引用正确率 | API 实检索（容器向量） | 同左 |
| 预测 MAE / WAPE | 确定性回测 | 同左 |
| 对话任务完成率 / 工具调用正确率 | 离线图（无 LLM） | 真实 LLM 图（模型+网络非确定） |
| P50/P95 延迟 | 每项实际采样 | 每项实际采样 |
| Token（输入/输出/总） | 不产生（None） | 真实调用时采样 |
| 降级次数 / 降级率 | 0（`LLM_MODE_OFFLINE` 是主动选择，不计入降级） | 按稳定降级原因统计 |
| 降级原因分布 | `{LLM_MODE_OFFLINE: n}` | 真实失败原因（`LLM_TIMEOUT` / `LLM_INVALID_RESPONSE` / `LLM_UNAVAILABLE` / `LLM_NOT_CONFIGURED`） |
| 总成本 / 平均每任务成本 | 不产生（None） | 配置 `LLM_PRICE_PER_1K_INPUT/OUTPUT` 时估算，否则为 `null` |
| 成本单价来源 / 是否假设单价 | 不适用 | `cost_price_source` + `cost_price_assumed=true` |

## 诚实边界

- 全部为固定随机种子生成的合成数据，结果不代表真实经营收益。
- 延迟为单线程小样本（样本量见 `*_sample_size`），不构成容量结论。
- 真实 LLM 模式结果受模型/网络影响，非确定性；offline 模式确定性可复现。
- **OFFLINE 模式强制禁用 LLM**：脚本会加载 `.env`（可能含 `LLM_API_KEY`），
  `offline` 模式下显式清空 LLM/Langfuse 凭证，确保对话走确定性离线图
  （`dialog_offline_mode=true`、Token 为 null、P50 毫秒级），不冒充真实模型。
- **成本是假设单价估算，不是供应商真实账单**：优先读取可配置单价
  （`LLM_PRICE_PER_1K_INPUT/OUTPUT`），**缺少单价时成本字段为 `null`（不写 0）**；
  报告用 `cost_price_assumed=true` 标注假设性质，生产环境必须用真实账单校准。
- 降级原因按稳定 code 聚合（`LLM_MODE_OFFLINE` / `LLM_NOT_CONFIGURED` / `LLM_TIMEOUT` /
  `LLM_INVALID_RESPONSE` / `LLM_UNAVAILABLE`）；OFFLINE 模式下的 `LLM_MODE_OFFLINE`
  是主动选择，不计入 `degradation_count`，但保留在 `degradation_reasons` 分布中。
- 本报告**不含** V1.1 HTTP Agent 主链路（`agent/start`、`agent/resume`、`agent/state`）
  与 MCP 只读 Server 的指标；这两项由 `tests/api/`、`tests/integration/test_mcp_tools.py`
  验证，不与黄金集指标混报。
- Langfuse：代码已实现，本地 mock 单测已验证；云端 trace 未验证
  （未配置 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`）。

## 黄金集构成（schema `stockmind-eval-v1`）

- `parameter_extraction`（10 例）：正常补货、缺参澄清、库外拒答、查询/解释意图。
- `rag_queries`（5 例）：安全库存、仓库特殊规则、到货异常、供应商约束、提示注入拒绝。
- `forecast_cases`（4 例）：算法选择、数据不足降级。
- `dialog_cases`（10 例）：草稿中断、缺参澄清、查询、解释、非业务输入和预算说明。

`llm` 模式必须以报告中的 `llm_configured` 和 `dialog_mode` 判断是否真的调用模型；缺少 Key
时的降级结果仅用于验证降级链路，不能写成真实 LLM 指标。

安全不变量（越权/重复建议/重复采购/重复入库/未知状态盲目重试/非法迁移/幂等键异载荷）
由 `.dev/closeout_invariants.sh` 专项检查，不在此报告内合并。
