// StockMind API 客户端：强类型请求/响应、actor 头、幂等键、request id
const ACTOR_KEY = "stockmind_actor";

export function getActor(): string {
  return localStorage.getItem(ACTOR_KEY) || "alice";
}

export function setActor(actor: string): void {
  localStorage.setItem(ACTOR_KEY, actor);
}

export function newIdemKey(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export interface ApiError {
  request_id: string;
  code: string;
  message: string;
  detail?: unknown;
}

export class ApiError extends Error {
  code: string;
  requestId: string;
  detail?: unknown;
  constructor(body: ApiError) {
    super(body.message);
    this.code = body.code;
    this.requestId = body.request_id;
    this.detail = body.detail;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  idemKey?: string
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Actor-Id": getActor(),
  };
  if (idemKey) headers["X-Idempotency-Key"] = idemKey;
  const resp = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = (await resp.json().catch(() => ({}))) as any;
  if (!resp.ok) {
    throw new ApiError(json);
  }
  return json.data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown, idemKey?: string) =>
    request<T>("POST", path, body, idemKey),
  put: <T>(path: string, body?: unknown, idemKey?: string) =>
    request<T>("PUT", path, body, idemKey),
};

// ---------- 类型 ----------
export interface UserDto {
  actor_id: string;
  display_name: string;
  roles: string[];
}

export interface PlanLineDto {
  line_id: string;
  product_id: string;
  flag: string;
  active_for_dedupe: boolean;
  exclusion_reason?: string | null;
  blocked_code?: string | null;
  blocked_reason?: string | null;
  order_qty: number;
  daily_forecast?: string | null;
  forecast_algorithm?: string | null;
  mae?: string | null;
  wape?: string | null;
  fallback_reason?: string | null;
  planning_window: number;
  coverage_demand: string;
  target_stock: string;
  available: string;
  inbound: string;
  net_demand: string;
  supplier_id?: string | null;
  supplier_reason?: string | null;
  decision_input_hash: string;
  intermediate?: Record<string, unknown>;
  rule_refs?: unknown[];
}

export interface PlanDto {
  plan_id: string;
  warehouse_id: string;
  thread_id?: string | null;
  trigger_type: string;
  status: string;
  requested_window: number;
  planning_date: string;
  revision_of_plan_id?: string | null;
  decision_version: number;
  version: number;
  lines?: PlanLineDto[];
}

export interface PurchaseOrderDto {
  purchase_order_id: string;
  warehouse_id: string;
  plan_id?: string | null;
  supplier_id: string;
  status: string;
  version: number;
  lines?: Array<{
    line_id: string;
    product_id: string;
    order_qty: number;
    received_qty: number;
    remaining: number;
    unit_price: string;
  }>;
  attempts?: Array<{ attempt_no: number; status: string; external_order_no?: string | null }>;
}

export interface ExecutionDto {
  execution_id: string;
  schedule_id?: string | null;
  trigger_type: string;
  status: string;
  scanned_count: number;
  draft_count: number;
  success_count: number;
  blocked_count: number;
  failed_count: number;
  error_summary?: string | null;
  items?: Array<{ warehouse_id: string; product_id: string; status: string; block_reason?: string | null }>;
}

export interface AlertDto {
  alert_id: string;
  alert_type: string;
  warehouse_id: string;
  product_id: string;
  blocker_code: string;
  status: string;
  message: string;
}

// V1 功能优化：只读数据视图类型
export interface InventoryDto {
  warehouse_id: string;
  product_id: string;
  on_hand: number;
  reserved: number;
  available: number;
  inbound_remaining: number;
  quant_version: number;
}

export interface DemandDayDto {
  day: string;
  demand: number;
  complete: boolean;
}

export interface DemandDto {
  warehouse_id: string;
  product_id: string;
  business_date: string;
  days: DemandDayDto[];
}

export interface SupplierRelationDto {
  product_id: string;
  price: string;
  lead_days: number;
  minimum_order_qty: number;
  pack_multiple: number;
  enabled: boolean;
  version: number;
}

export interface SupplierDto {
  supplier_id: string;
  name: string;
  business_priority: number;
  currency: string;
  relations: SupplierRelationDto[];
}

export interface ProductDto {
  product_id: string;
  name: string;
  category: string;
}

export interface WarehouseDto {
  warehouse_id: string;
  name: string;
  timezone: string;
}
