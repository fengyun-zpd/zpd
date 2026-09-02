import { Fragment, useCallback, useEffect, useState } from "react";
import { api, PlanDto, PlanLineDto } from "../api/client";
import {
  formatBlockerCode,
  formatDecimal,
  formatForecast,
  formatLineFlag,
  formatQuantity,
  formatStatus,
  formatTrigger,
  listLabel,
  shortId,
} from "../ui/format";

interface Expanded {
  planId: string;
  lines: PlanLineDto[];
  error: string;
}

const BLOCK_NEXT_STEP: Record<string, string> = {
  no_rule: "请核对仓库/SKU 是否在规则知识库范围内，或联系管理员补全规则。",
  rule_conflict: "请人工核对规则版本与适用范围，排除冲突后重算。",
  illegal_input: "请先修正基础数据（库存、预留量、交期或规则值不合法）。",
  no_supplier: "请先在供应商关系维护中补充该 SKU 的供应商。",
  data_insufficient: "历史需求覆盖不完整，可按固定安全库存降级或补充数据后重算。",
  ACTIVE_REPLENISHMENT_EXISTS: "请先在审批箱处理已有活动建议，无需重复发起。",
};

export function Workbench({ onSelectPlan }: { onSelectPlan?: (planId: string) => void }) {
  const [plans, setPlans] = useState<PlanDto[]>([]);
  const [error, setError] = useState<string>("");
  const [expanded, setExpanded] = useState<Expanded | null>(null);

  const load = useCallback(() => {
    api
      .get<PlanDto[]>("/api/v1/plans")
      .then(setPlans)
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(load, [load]);

  const toggleDetail = (planId: string) => {
    if (expanded?.planId === planId) {
      setExpanded(null);
      return;
    }
    api
      .get<PlanDto>(`/api/v1/plans/${planId}`)
      .then((d) => setExpanded({ planId, lines: d.lines || [], error: "" }))
      .catch((e) => setExpanded({ planId, lines: [], error: (e as Error).message }));
  };

  return (
    <div className="card">
      <h2>补货工作台</h2>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>计划</th>
            <th>仓库</th>
            <th>触发方式</th>
            <th>状态</th>
            <th>明细统计</th>
            <th>窗口</th>
            <th>版本</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {plans.length === 0 && (
            <tr>
              <td colSpan={8} className="empty-cell">
                当前没有计划。先到“补货助手”生成草稿，或在“定时任务”执行扫描。
              </td>
            </tr>
          )}
          {plans.map((p) => {
            const purchasable = p.purchasable_count ?? 0;
            const zeroQty = p.zero_qty_count ?? 0;
            const hasLines = purchasable + zeroQty > 0;
            return (
              <Fragment key={p.plan_id}>
                <tr>
                  <td title={p.plan_id}>
                    {listLabel(p.plan_id, [p.warehouse_id, formatStatus(p.status), `v${p.version}`])}
                  </td>
                  <td>{p.warehouse_id}</td>
                  <td>{formatTrigger(p.trigger_type)}</td>
                  <td>
                    <span className={`badge status-${p.status}`}>{formatStatus(p.status)}</span>
                  </td>
                  <td>
                    {!hasLines && <span className="hint">-</span>}
                    {hasLines && (
                      <span>
                        可采购 <strong>{purchasable}</strong> 条 · 无需采购{" "}
                        <strong>{zeroQty}</strong> 条
                        {purchasable === 0 && (
                          <span className="hint">（该计划无需建单）</span>
                        )}
                      </span>
                    )}
                  </td>
                  <td>{p.requested_window} 天</td>
                  <td>v{p.version}</td>
                  <td>
                    <button onClick={() => toggleDetail(p.plan_id)}>
                      {expanded?.planId === p.plan_id ? "收起详情" : "查看详情"}
                    </button>
                    {p.status === "pending_approval" && (
                      <button onClick={() => onSelectPlan?.(p.plan_id)}>打开审批</button>
                    )}
                  </td>
                </tr>
              {expanded?.planId === p.plan_id && (
                <tr>
                  <td colSpan={8}>
                    <div className="plan-detail">
                      <h4>计划 {shortId(p.plan_id)} · SKU 明细</h4>
                      {expanded.error && <p className="error">{expanded.error}</p>}
                      {expanded.lines.length === 0 && (
                        <p className="hint">该计划没有明细（无有效建议或明细为空）。</p>
                      )}
                      <table>
                        <thead>
                          <tr>
                            <th>SKU</th>
                            <th>标记</th>
                            <th>建议数量</th>
                            <th>供应商</th>
                            <th>预测算法</th>
                            <th>日预测</th>
                            <th>净需求</th>
                            <th>阻断/下一步</th>
                          </tr>
                        </thead>
                        <tbody>
                          {expanded.lines.map((l) => {
                            const needsNoPurchase = l.flag === "valid" && l.order_qty <= 0;
                            const purchasable = l.flag === "valid" && l.order_qty > 0;
                            const badgeClass = needsNoPurchase
                              ? "status-no_purchase"
                              : purchasable
                                ? "status-success"
                                : l.flag === "blocked"
                                  ? "status-blocked"
                                  : "status-failed";
                            const badgeText = needsNoPurchase
                              ? "无需采购"
                              : purchasable
                                ? "有效（可采购）"
                                : formatLineFlag(l.flag);
                            return (
                              <tr key={l.line_id}>
                                <td>{l.product_id}</td>
                                <td>
                                  <span className={`badge ${badgeClass}`} title={l.flag || undefined}>
                                    {badgeText}
                                  </span>
                                </td>
                                <td>{formatQuantity(l.order_qty)}</td>
                                <td>{l.supplier_id || "-"}</td>
                                <td title={l.forecast_algorithm || undefined}>
                                  {formatForecast(l.forecast_algorithm)}
                                </td>
                                <td>{formatDecimal(l.daily_forecast)}</td>
                                <td>{formatDecimal(l.net_demand)}</td>
                                <td>
                                  {l.flag === "blocked" ? (
                                    <div className="hint">
                                      {l.blocked_code ? (
                                        <span title={l.blocked_code}>{formatBlockerCode(l.blocked_code)}</span>
                                      ) : (
                                        "已阻断"
                                      )}
                                      {l.blocked_reason && <span>：{l.blocked_reason}</span>}
                                      {l.blocked_code && BLOCK_NEXT_STEP[l.blocked_code] && (
                                        <div className="chat-next-step">
                                          下一步：{BLOCK_NEXT_STEP[l.blocked_code]}
                                        </div>
                                      )}
                                    </div>
                                  ) : (
                                    <span className="hint">
                                      {needsNoPurchase
                                        ? "建议数量为 0：当前库存或在途已满足需求，无需采购"
                                        : "-"}
                                    </span>
                                  )}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          );
          })}
        </tbody>
      </table>
    </div>
  );
}
