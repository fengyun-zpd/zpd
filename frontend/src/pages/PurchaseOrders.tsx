import { useCallback, useEffect, useState } from "react";
import { api, newIdemKey, PlanDto, PurchaseOrderDto } from "../api/client";
import { formatQuantity, formatStatus, listLabel, shortId } from "../ui/format";

export function PurchaseOrders() {
  const [orders, setOrders] = useState<PurchaseOrderDto[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<PurchaseOrderDto | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api
      .get<PurchaseOrderDto[]>("/api/v1/purchase-orders")
      .then(setOrders)
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    api
      .get<PurchaseOrderDto>(`/api/v1/purchase-orders/${selected}`)
      .then(setDetail)
      .catch((e) => setError((e as Error).message));
  }, [selected]);

  const act = async (action: string, poId: string, extra?: Record<string, unknown>) => {
    setError("");
    try {
      await api.post(`/api/v1/purchase-orders/${poId}/${action}`, extra || {}, newIdemKey());
      const refreshed = await api.get<PurchaseOrderDto>(`/api/v1/purchase-orders/${poId}`);
      setDetail(refreshed);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const receive = async (lineId: string) => {
    const qty = Number(prompt("本次收货数量：", "1"));
    if (!qty || qty <= 0) return;
    setError("");
    try {
      await api.post(
        `/api/v1/purchase-order-lines/${lineId}/receive`,
        { receipt_event_id: newIdemKey(), qty },
        newIdemKey()
      );
      const refreshed = await api.get<PurchaseOrderDto>(`/api/v1/purchase-orders/${selected}`);
      setDetail(refreshed);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // 按供应商建单（已批准计划）—— 补齐浏览器闭环的最后一步
  const [approvedPlans, setApprovedPlans] = useState<PlanDto[]>([]);
  const [createPlanId, setCreatePlanId] = useState("");

  useEffect(() => {
    api
      .get<PlanDto[]>("/api/v1/plans?status=approved")
      .then((plans) => setApprovedPlans(plans))
      .catch(() => setApprovedPlans([]));
  }, [orders]);

  // 只允许：至少存在一条 order_qty>0 的可采购明细，且尚未建单
  const selectablePlans = approvedPlans.filter(
    (p) => (p.purchasable_count ?? 0) > 0 && !p.has_po
  );
  const zeroOnlyPlans = approvedPlans.filter((p) => (p.purchasable_count ?? 0) === 0 && !p.has_po);
  const alreadyOrderedPlans = approvedPlans.filter((p) => p.has_po);

  const createPOs = async () => {
    if (!createPlanId) return;
    setError("");
    try {
      await api.post(`/api/v1/plans/${createPlanId}/purchase-orders`, {}, newIdemKey());
      setCreatePlanId("");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="card">
      <h2>采购单</h2>
      {error && <p className="error">{error}</p>}
      <div className="create-po">
        <span>已批准计划（可建单）：</span>
        <select value={createPlanId} onChange={(e) => setCreatePlanId(e.target.value)}>
          <option value="">选择计划…</option>
          {selectablePlans.map((p) => (
            <option key={p.plan_id} value={p.plan_id} title={p.plan_id}>
              {listLabel(p.plan_id, [p.warehouse_id, formatStatus(p.status), `v${p.version}`])}
            </option>
          ))}
        </select>
        <button onClick={createPOs} disabled={!createPlanId}>
          创建采购单
        </button>
      </div>
      {selectablePlans.length === 0 && (
        <p className="empty-state">
          当前没有可建单的已批准计划（下拉只列出至少存在一条正建议数量明细的计划）。
        </p>
      )}
      {(zeroOnlyPlans.length > 0 || alreadyOrderedPlans.length > 0) && (
        <p className="hint">
          {zeroOnlyPlans.length > 0 &&
            `${zeroOnlyPlans.length} 个已批准计划全部明细建议数量为 0（该计划无需建单：净需求已由库存或在途满足）。`}
          {alreadyOrderedPlans.length > 0 &&
            `${alreadyOrderedPlans.length} 个已批准计划已创建过采购单（不允许重复建单）。`}
        </p>
      )}
      <select value={selected} onChange={(e) => setSelected(e.target.value)}>
        <option value="">选择采购单…</option>
        {orders.map((o) => (
          <option key={o.purchase_order_id} value={o.purchase_order_id} title={o.purchase_order_id}>
            {listLabel(o.purchase_order_id, [o.supplier_id, formatStatus(o.status)])}
          </option>
        ))}
      </select>
      {orders.length === 0 && <p className="empty-state">当前没有采购单。请先批准计划，再创建采购单。</p>}

      {detail && (
        <div className="po-detail">
          <h3>采购单 {shortId(detail.purchase_order_id)} · {detail.supplier_id}</h3>
          <p>
            状态：
            <span className={`badge status-${detail.status}`}>{formatStatus(detail.status)}</span>
          </p>
          <table>
            <thead>
              <tr>
                <th>SKU</th>
                <th>订单量</th>
                <th>已收</th>
                <th>剩余</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {(detail.lines || []).map((l) => (
                <tr key={l.line_id}>
                  <td>{l.product_id}</td>
                  <td>{formatQuantity(l.order_qty)}</td>
                  <td>{formatQuantity(l.received_qty)}</td>
                  <td>{formatQuantity(l.remaining)}</td>
                  <td>
                    {(detail.status === "ordered" || detail.status === "partially_received") && (
                      <button onClick={() => receive(l.line_id)}>登记到货</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="actions">
            {detail.status === "po_created" && (
              <>
                <button onClick={() => act("place", detail.purchase_order_id)}>下达（模拟供应商）</button>
                <button className="danger" onClick={() => act("cancel", detail.purchase_order_id)}>
                  取消（未下达）
                </button>
              </>
            )}
            {detail.status === "order_unknown" && (
              <>
                <p className="hint" style={{ color: "var(--warn)" }}>
                  外部订单状态未知：只能通过查询恢复，不允许盲目重试或换键重下。
                </p>
                <button onClick={() => act("query", detail.purchase_order_id)}>查询外部订单状态</button>
              </>
            )}
            {detail.status === "received" && (
              <button onClick={() => act("close", detail.purchase_order_id)}>确认关闭</button>
            )}
          </div>
          {(detail.attempts?.length || 0) > 0 && (
            <div className="evidence">
              <h4>下单尝试记录</h4>
              <pre>{JSON.stringify(detail.attempts, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
