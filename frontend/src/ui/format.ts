const STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  pending_approval: "待审批",
  approved: "已批准",
  rejected: "已驳回",
  superseded: "已被替代",
  po_created: "已建采购单",
  ordering: "下单中",
  ordered: "已下达",
  order_unknown: "订单状态未知",
  partially_received: "部分到货",
  received: "已收齐",
  closed: "已关闭",
  cancelled: "已取消",
  running: "执行中",
  succeeded: "执行成功",
  failed: "执行失败",
  blocked: "已阻断",
  success: "成功",
};

const TRIGGER_LABELS: Record<string, string> = {
  manual: "手动",
  scheduled: "定时",
  rerun: "手动重跑",
};

const ROLE_LABELS: Record<string, string> = {
  operator: "操作员",
  approver: "审批员",
  buyer: "采购员",
  admin: "管理员",
  system: "内部服务主体",
};

const ERROR_LABELS: Record<string, string> = {
  ACTIVE_REPLENISHMENT_EXISTS: "存在活动补货建议",
  PLAN_STALE: "计划依据已变化",
  IDEMPOTENCY_KEY_REUSED: "幂等键载荷不一致",
  RESOURCE_BUSY: "资源正在处理中",
  no_supplier: "没有可用供应商",
  no_rule: "没有适用规则",
  rule_conflict: "规则冲突",
  illegal_input: "输入数据不合法",
  runtime_error: "运行错误",
  DATA_UNAVAILABLE: "业务数据不可用",
  FORBIDDEN: "无权执行此操作",
  NOT_FOUND: "资源不存在",
  VALIDATION_ERROR: "请求参数不正确",
  INTERNAL_ERROR: "服务器内部错误",
  NETWORK_ERROR: "网络连接失败",
  invalid_rule_type: "规则类型不支持",
  unknown: "未分类问题",
};

const RULE_TYPE_LABELS: Record<string, string> = {
  safety_stock: "安全库存",
  replenishment_policy: "补货策略",
};

const SCOPE_LABELS: Record<string, string> = {
  product: "SKU",
  category: "商品分类",
  warehouse: "仓库",
  global: "全局",
};

export function formatStatus(value: string | null | undefined): string {
  if (!value) return "未提供";
  return STATUS_LABELS[value] ?? value;
}

export function formatTrigger(value: string | null | undefined): string {
  if (!value) return "未提供";
  return TRIGGER_LABELS[value] ?? value;
}

export function formatRole(value: string | null | undefined): string {
  if (!value) return "未分配角色";
  return ROLE_LABELS[value] ?? value;
}

export function formatErrorCode(value: string | null | undefined): string {
  if (!value) return "未知问题";
  return ERROR_LABELS[value] ?? `未知问题（${value}）`;
}

export function formatRuleType(value: string | null | undefined): string {
  if (!value) return "未提供";
  return RULE_TYPE_LABELS[value] ?? value;
}

export function formatScope(value: string | null | undefined): string {
  if (!value) return "未提供";
  return SCOPE_LABELS[value] ?? value;
}

export function formatBoolean(value: boolean): string {
  return value ? "启用" : "停用";
}

export function formatNumber(
  value: string | number | null | undefined,
  maximumFractionDigits = 2,
): string {
  if (value === null || value === undefined || value === "") return "-";
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits,
    useGrouping: false,
  }).format(numeric);
}

export function formatQuantity(value: string | number | null | undefined): string {
  return formatNumber(value, 0);
}

// 十进制字段（预测/需求/库存等）统一最多 4 位、最小展示 2 位；
// 极小值（如 1e-12）归一为 0，避免页面出现 0E-12 / 12 位无意义小数。
export function formatDecimal(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const abs = Math.abs(numeric);
  if (abs > 0 && abs < 0.0001) return "0";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 4,
    useGrouping: false,
  }).format(numeric);
}

// ---------- 预测算法 ----------
const FORECAST_LABELS: Record<string, string> = {
  wma_28_v1: "28日加权移动平均",
  ses_a03_v1: "指数平滑",
};

export function formatForecast(value: string | null | undefined): string {
  if (!value) return "降级";
  return FORECAST_LABELS[value] ?? value;
}

// ---------- 计划明细标记 ----------
const LINE_FLAG_LABELS: Record<string, string> = {
  valid: "有效",
  excluded: "已排除",
  blocked: "已阻断",
};

export function formatLineFlag(value: string | null | undefined): string {
  if (!value) return "-";
  return LINE_FLAG_LABELS[value] ?? value;
}

// ---------- 告警类型 ----------
const ALERT_TYPE_LABELS: Record<string, string> = {
  blocked: "已阻断",
  low_stock: "库存偏低",
  failed: "执行失败",
  system: "系统提示",
};

export function formatAlertType(value: string | null | undefined): string {
  if (!value) return "-";
  return ALERT_TYPE_LABELS[value] ?? value;
}

// ---------- 告警状态 ----------
export function formatAlertStatus(value: string | null | undefined): string {
  if (value === "open") return "未处理";
  if (value === "closed") return "已处理";
  return value || "-";
}

// ---------- 阻断码（主界面中文，原始码可作 title） ----------
export function formatBlockerCode(value: string | null | undefined): string {
  if (!value) return "-";
  // 优先用已有错误码映射；再补 plan_service 使用的阻断码
  const overrides: Record<string, string> = {
    ACTIVE_REPLENISHMENT_EXISTS: "存在活动补货建议",
    no_supplier: "没有可用供应商",
    no_rule: "没有适用规则",
    rule_conflict: "规则冲突",
    illegal_input: "输入数据不合法",
    data_insufficient: "数据不足",
    invalid_rule_type: "规则类型不支持",
    runtime_error: "计算异常",
  };
  return overrides[value] ?? ERROR_LABELS[value] ?? value;
}

// ---------- 短 ID（消除 SEED-PLA… 混淆：追加后 4 位） ----------
export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return "-";
  if (value.length <= length) return value;
  // SEED-PLAN-1 这类语义前缀：保留前缀 + 后 4 位，避免多个都显示成 SEED-PLA…
  const tail = value.slice(-4);
  return `${value.slice(0, length)}…${tail}`;
}

// ---------- 列表项标识：短 ID + 附加维度（仓库/状态/版本等） ----------
export function listLabel(
  value: string | null | undefined,
  extra: Array<string | number | null | undefined>,
  length = 8,
): string {
  const base = shortId(value, length);
  const parts = extra.filter((x) => x !== null && x !== undefined && String(x) !== "");
  return parts.length ? `${base} · ${parts.join(" · ")}` : base;
}
