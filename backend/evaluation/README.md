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
| 单次运行成本 | 不产生（None） | 配置 `LLM_PRICE_PER_1K_INPUT/OUTPUT` 时估算，否则仅报 Token |

## 诚实边界

- 全部为固定随机种子生成的合成数据，结果不代表真实经营收益。
- 延迟为单线程小样本（样本量见 `*_sample_size`），不构成容量结论。
- 真实 LLM 模式结果受模型/网络影响，非确定性；offline 模式确定性可复现。
- **OFFLINE 模式强制禁用 LLM**：脚本会加载 `.env`（可能含 `LLM_API_KEY`），
  `offline` 模式下显式清空 LLM/Langfuse 凭证，确保对话走确定性离线图
  （`dialog_offline_mode=true`、Token 为 null、P50 毫秒级），不冒充真实模型。
- 成本优先读取可配置单价（`LLM_PRICE_PER_1K_INPUT/OUTPUT`），缺少单价时只报告 Token。
- Langfuse：代码已实现，本地 mock 单测已验证；云端 trace 未验证
  （未配置 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`）。

## 黄金集构成（schema `stockmind-eval-v1`）

- `parameter_extraction`（7 例）：正常补货、缺参澄清、库外拒答、查询/解释意图。
- `rag_queries`（5 例）：安全库存、仓库特殊规则、到货异常、供应商约束、提示注入拒绝。
- `forecast_cases`（4 例）：算法选择、数据不足降级。
- `dialog_cases`（3 例）：草稿中断、缺参澄清、查询意图。

安全不变量（越权/重复建议/重复采购/重复入库/未知状态盲目重试/非法迁移/幂等键异载荷）
由 `.dev/closeout_invariants.sh` 专项检查，不在此报告内合并。
