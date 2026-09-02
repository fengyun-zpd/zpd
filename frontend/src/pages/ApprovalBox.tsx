import { useCallback, useEffect, useState } from "react";
import { api, newIdemKey, ApiError, PlanDto } from "../api/client";
import { formatDecimal, formatErrorCode, formatForecast, formatQuantity, listLabel, shortId } from "../ui/format";

export function ApprovalBox({
  onChanged,
  initialPlanId,
}: {
  onChanged?: () => void;
  initialPlanId?: string;
}) {
  const [plans, setPlans] = useState<PlanDto[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [detail, setDetail] = useState<PlanDto | null>(null);
  const [decision, setDecision] = useState<Record<string, "approve" | "exclude">>({});
  const [error, setError] = useState("");
  const [errorCode, setErrorCode] = useState("");
  const [errorDetail, setErrorDetail] = useState<string>("");

  const showError = (e: unknown) => {
    if (e instanceof ApiError) {
      setError(e.message);
      setErrorCode(e.code);
      setErrorDetail(
        e.detail && typeof e.detail === "object" ? JSON.stringify(e.detail, null, 2) : ""
      );
    } else {
      setError((e as Error).message);
      setErrorCode("");
      setErrorDetail("");
    }
  };

  const load = useCallback(() => {
    api
      .get<PlanDto[]>("/api/v1/plans?status=pending_approval")
      .then(setPlans)
      .catch((e) => showError(e));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    if (initialPlanId && plans.some((plan) => plan.plan_id === initialPlanId)) {
      setSelected(initialPlanId);
    }
  }, [initialPlanId, plans]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    api
      .get<PlanDto>(`/api/v1/plans/${selected}`)
      .then((d) => {
        setDetail(d);
        const init: Record<string, "approve" | "exclude"> = {};
        (d.lines || [])
          .filter((l) => l.flag === "valid")
          .forEach((l) => (init[l.line_id] = "approve"));
        setDecision(init);
        setErrorCode("");
        setErrorDetail("");
      })
      .catch((e) => showError(e));
  }, [selected]);

  const validLines = (detail?.lines || []).filter((l) => l.flag === "valid");

  const submit = async (mode: "approve" | "reject") => {
    if (!detail) return;
    setError("");
    setErrorCode("");
    setErrorDetail("");
    try {
      if (mode === "reject") {
        await api.post(`/api/v1/plans/${detail.plan_id}/reject`, {}, newIdemKey());
      } else {
        await api.post(
          `/api/v1/plans/${detail.plan_id}/approve`,
          { decisions: decision, expected_version: detail.version },
          newIdemKey()
        );
      }
      setSelected("");
      load();
      onChanged?.();
    } catch (e) {
      showError(e);
    }
  };

  return (
    <div className="card">
      <h2>审批箱</h2>
      {error && <p className="error">{error}</p>}
      {errorCode && (
        <div className="chat-detail">
          <div>
            <strong>{formatErrorCode(errorCode)}（{errorCode}）</strong>
            {errorCode === "PLAN_STALE" && (
              <div className="chat-next-step">
                决策输入已变化：请查看下方变化字段，可排除变化明细后重新提交；如需重算，请在补货助手重新发起修订。
              </div>
            )}
            {errorCode === "IDEMPOTENCY_KEY_REUSED" && (
              <div className="chat-next-step">同一幂等键被用于不同载荷，已拒绝；请刷新页面重新操作。</div>
            )}
            {errorCode === "RESOURCE_BUSY" && (
              <div className="chat-next-step">资源忙，请稍后重试（同一次操作将复用幂等键）。</div>
            )}
          </div>
          {errorDetail && <pre className="error-detail">{errorDetail}</pre>}
        </div>
      )}
      <select value={selected} onChange={(e) => setSelected(e.target.value)}>
        <option value="">选择待审批计划…</option>
        {plans.map((p) => (
          <option key={p.plan_id} value={p.plan_id} title={p.plan_id}>
            {listLabel(p.plan_id, [p.warehouse_id, `窗口${p.requested_window}`, `v${p.version}`])}
          </option>
        ))}
      </select>
      {plans.length === 0 && <p className="empty-state">当前没有待审批计划。请先生成补货草稿或执行定时扫描。</p>}

      {detail && (
        <div className="approval-detail">
          <h3>计划 {shortId(detail.plan_id)}（版本 v{detail.version}）</h3>
          <table>
            <thead>
              <tr>
                <th>SKU</th>
                <th>建议数量</th>
                <th>供应商</th>
                <th>预测算法</th>
                <th>日预测</th>
                <th>目标库存</th>
                <th>净需求</th>
                <th>决策</th>
              </tr>
            </thead>
            <tbody>
              {validLines.map((l) => (
                <tr key={l.line_id}>
                  <td>{l.product_id}</td>
                  <td>{formatQuantity(l.order_qty)}</td>
                  <td>{l.supplier_id || "-"}</td>
                  <td title={l.forecast_algorithm || undefined}>{formatForecast(l.forecast_algorithm)}</td>
                  <td>{formatDecimal(l.daily_forecast)}</td>
                  <td>{formatDecimal(l.target_stock)}</td>
                  <td>{formatDecimal(l.net_demand)}</td>
                  <td>
                    <select
                      value={decision[l.line_id] || "exclude"}
                      onChange={(e) =>
                        setDecision((d) => ({
                          ...d,
                          [l.line_id]: e.target.value as "approve" | "exclude",
                        }))
                      }
                    >
                      <option value="approve">批准</option>
                      <option value="exclude">排除</option>
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {validLines.length === 0 && (
            <div className="empty-state">
              <p>该计划没有可审批明细，请重新生成补货草稿。</p>
              {(detail?.lines || []).some((l) => l.flag === "blocked") && (
                <div className="chat-detail">
                  {(detail?.lines || [])
                    .filter((l) => l.flag === "blocked")
                    .map((l) => (
                      <div key={l.line_id} className="chat-block-line">
                        <div>
                          <strong>{l.product_id}</strong>
                          {l.blocked_code ? (
                            <span title={l.blocked_code}> [{formatErrorCode(l.blocked_code)}]</span>
                          ) : (
                            ""
                          )}
                          ：{l.blocked_reason}
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}
          {validLines.length > 0 && (
            <>
              <div className="evidence">
                <h4>公式与证据（决策输入哈希 {validLines[0]?.decision_input_hash?.slice(0, 16)}…）</h4>
                <pre>
                  {validLines
                    .map((l) => `${l.product_id}: 覆盖需求=${formatDecimal(l.coverage_demand)} 目标库存=${formatDecimal(l.target_stock)} 可用=${formatDecimal(l.available)} 在途=${formatDecimal(l.inbound)} 净需求=${formatDecimal(l.net_demand)} → 建议量=${formatQuantity(l.order_qty)}`)
                    .join("\n")}
                </pre>
              </div>
              <div className="actions">
                <button onClick={() => submit("approve")}>提交审批</button>
                <button className="danger" onClick={() => submit("reject")}>
                  整单驳回
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
