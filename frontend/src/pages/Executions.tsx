import { useCallback, useEffect, useState } from "react";
import { api, ExecutionDto } from "../api/client";
import { formatBlockerCode, formatNumber, formatStatus, formatTrigger, listLabel } from "../ui/format";

/** 页面默认最多展示最近 50 条执行记录（不改数据库与审计语义，仅限制列表渲染量）。 */
const MAX_LIST = 50;

export function Executions() {
  const [executions, setExecutions] = useState<ExecutionDto[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<ExecutionDto | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    api
      .get<ExecutionDto[]>("/api/v1/executions")
      .then(setExecutions)
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    setError("");
    api
      .get<ExecutionDto>(`/api/v1/executions/${selected}`)
      .then(setDetail)
      .catch((e) => setError((e as Error).message));
  }, [selected]);

  const visible = executions.slice(0, MAX_LIST);

  return (
    <div className="card">
      <h2>执行记录</h2>
      {error && <p className="error">{error}</p>}
      {loading && executions.length === 0 && <p className="hint">正在加载执行记录…</p>}
      {!loading && executions.length === 0 && !error && (
        <p className="empty-state">当前没有执行记录。请在“定时任务”中立即执行一次扫描。</p>
      )}
      {visible.length > 0 && (
        <>
          <select value={selected} onChange={(e) => setSelected(e.target.value)}>
            <option value="">选择执行记录…</option>
            {visible.map((e) => (
              <option key={e.execution_id} value={e.execution_id} title={e.execution_id}>
                {listLabel(e.execution_id, [formatTrigger(e.trigger_type), formatStatus(e.status)])}
              </option>
            ))}
          </select>
          {executions.length > MAX_LIST && (
            <p className="hint">列表仅展示最近 {MAX_LIST} 条；完整历史可在 API 审计中查询。</p>
          )}
        </>
      )}
      {detail && (
        <div className="exec-detail">
          <h3 title={detail.execution_id}>
            执行 {detail.execution_id.slice(0, 8)}…{detail.execution_id.slice(-4)}
          </h3>
          <p>
            状态：{formatStatus(detail.status)} · 扫描 {formatNumber(detail.scanned_count, 0)} · 草稿{" "}
            {formatNumber(detail.draft_count, 0)} · 成功 {formatNumber(detail.success_count, 0)} · 阻断{" "}
            {formatNumber(detail.blocked_count, 0)} · 失败 {formatNumber(detail.failed_count, 0)}
          </p>
          {detail.error_summary && <p className="error">{detail.error_summary}</p>}
          <table>
            <thead>
              <tr>
                <th>仓库</th>
                <th>SKU</th>
                <th>结果</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {(detail.items || []).map((it) => (
                <tr key={`${it.warehouse_id}-${it.product_id}`}>
                  <td>{it.warehouse_id}</td>
                  <td>{it.product_id}</td>
                  <td>
                    <span className={`badge status-${it.status}`}>{formatStatus(it.status)}</span>
                  </td>
                  <td title={it.block_reason || undefined}>
                    {it.block_reason ? formatBlockerCode(it.block_reason) : "-"}
                  </td>
                </tr>
              ))}
              {(detail.items || []).length === 0 && (
                <tr>
                  <td colSpan={4} className="empty-cell">
                    该执行记录没有逐 SKU 明细。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
