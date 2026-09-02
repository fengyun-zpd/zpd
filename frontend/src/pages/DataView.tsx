import { useCallback, useEffect, useState } from "react";
import {
  api,
  AlertDto,
  InventoryDto,
  ProductDto,
  SupplierDto,
  WarehouseDto,
} from "../api/client";
import { formatAlertStatus, formatAlertType, formatBlockerCode, formatDecimal, formatQuantity } from "../ui/format";

type Tab = "inventory" | "suppliers" | "alerts";

/** V1 只读数据视图：库存 / 需求 / 供应商 / 告警（仅展示，不产生任何副作用）。 */
export function DataView() {
  const [tab, setTab] = useState<Tab>("inventory");
  const [error, setError] = useState("");

  // 库存查询参数
  const [warehouses, setWarehouses] = useState<WarehouseDto[]>([]);
  const [products, setProducts] = useState<ProductDto[]>([]);
  const [wh, setWh] = useState("");
  const [pid, setPid] = useState("");
  const [inventory, setInventory] = useState<InventoryDto | null>(null);

  // 供应商 / 告警
  const [suppliers, setSuppliers] = useState<SupplierDto[]>([]);
  const [alerts, setAlerts] = useState<AlertDto[]>([]);

  useEffect(() => {
    api
      .get<WarehouseDto[]>("/api/v1/warehouses")
      .then((d) => {
        setWarehouses(d);
        if (d.length) setWh(d[0].warehouse_id);
      })
      .catch((e) => setError((e as Error).message));
    api
      .get<ProductDto[]>("/api/v1/products")
      .then((d) => {
        setProducts(d);
        if (d.length) setPid(d[0].product_id);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  const loadSuppliers = useCallback(() => {
    api
      .get<SupplierDto[]>("/api/v1/suppliers")
      .then(setSuppliers)
      .catch((e) => setError((e as Error).message));
  }, []);

  const loadAlerts = useCallback(() => {
    api
      .get<AlertDto[]>("/api/v1/alerts")
      .then(setAlerts)
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => {
    if (tab === "suppliers") loadSuppliers();
    if (tab === "alerts") loadAlerts();
  }, [tab, loadSuppliers, loadAlerts]);

  const queryInventory = () => {
    if (!wh || !pid) return;
    setError("");
    api
      .get<InventoryDto>(`/api/v1/inventory/${wh}/${pid}`)
      .then(setInventory)
      .catch((e) => setError((e as Error).message));
  };

  return (
    <div className="card">
      <h2>库存与数据</h2>
      {error && <p className="error">{error}</p>}
      <div className="nav-row">
        <button className={tab === "inventory" ? "nav-btn active" : "nav-btn"} onClick={() => setTab("inventory")}>
          库存查询
        </button>
        <button className={tab === "suppliers" ? "nav-btn active" : "nav-btn"} onClick={() => setTab("suppliers")}>
          供应商
        </button>
        <button className={tab === "alerts" ? "nav-btn active" : "nav-btn"} onClick={() => setTab("alerts")}>
          告警
        </button>
      </div>

      {tab === "inventory" && (
        <div>
          <div className="row">
            <select value={wh} onChange={(e) => setWh(e.target.value)}>
              {warehouses.map((w) => (
                <option key={w.warehouse_id} value={w.warehouse_id}>
                  {w.name} ({w.warehouse_id})
                </option>
              ))}
            </select>
            <select value={pid} onChange={(e) => setPid(e.target.value)}>
              {products.map((p) => (
                <option key={p.product_id} value={p.product_id}>
                  {p.product_id} · {p.name}
                </option>
              ))}
            </select>
            <button onClick={queryInventory}>查询库存</button>
          </div>
          {inventory && (
            <table>
              <thead>
                <tr>
                  <th>仓库</th>
                  <th>SKU</th>
                  <th>现存量</th>
                  <th>预留量</th>
                  <th>可用量</th>
                  <th>在途剩余</th>
                  <th>版本</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{inventory.warehouse_id}</td>
                  <td>{inventory.product_id}</td>
                  <td>{formatQuantity(inventory.on_hand)}</td>
                  <td>{formatQuantity(inventory.reserved)}</td>
                  <td>{formatQuantity(inventory.available)}</td>
                  <td>{formatQuantity(inventory.inbound_remaining)}</td>
                  <td>v{inventory.quant_version}</td>
                </tr>
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === "suppliers" && (
        <table>
          <thead>
            <tr>
              <th>供应商</th>
              <th>名称</th>
              <th>优先级</th>
              <th>币种</th>
              <th>SKU 关系</th>
            </tr>
          </thead>
          <tbody>
            {suppliers.map((s) => (
              <tr key={s.supplier_id}>
                <td>{s.supplier_id}</td>
                <td>{s.name}</td>
                <td>{s.business_priority}</td>
                <td>{s.currency}</td>
                <td>
                  {s.relations.map((r) => (
                    <div key={r.product_id} className="hint">
                      {r.product_id}: ¥{formatDecimal(r.price)} · 交期{formatQuantity(r.lead_days)}天 · 最小
                      {formatQuantity(r.minimum_order_qty)} · 整箱{formatQuantity(r.pack_multiple)}
                      {r.enabled ? "" : " [停用]"}
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {tab === "alerts" && (
        <table>
          <thead>
            <tr>
              <th>仓库</th>
              <th>SKU</th>
              <th>类型</th>
              <th>阻断码</th>
              <th>状态</th>
              <th>消息</th>
            </tr>
          </thead>
          <tbody>
            {alerts.length === 0 && (
              <tr>
                <td colSpan={6} className="empty-cell">
                  当前没有告警。
                </td>
              </tr>
            )}
            {alerts.map((a) => (
              <tr key={a.alert_id} title={`${a.alert_id}`}>
                <td>{a.warehouse_id}</td>
                <td>{a.product_id}</td>
                <td title={a.alert_type}>{formatAlertType(a.alert_type)}</td>
                <td title={a.blocker_code}>{formatBlockerCode(a.blocker_code)}</td>
                <td>
                  <span className={`badge ${a.status === "open" ? "status-blocked" : "status-success"}`}>
                    {formatAlertStatus(a.status)}
                  </span>
                </td>
                <td>{a.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
