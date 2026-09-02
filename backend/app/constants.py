"""业务常量：角色、状态、错误码、允许转换表、哈希与数值规范。

规范性定义来自《StockMind需求规格说明书》V4.1 与《StockMind架构设计文档》V4.1，
字段名保持英文原名。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 角色
ROLE_OPERATOR = "operator"
ROLE_APPROVER = "approver"
ROLE_BUYER = "buyer"
ROLE_SYSTEM = "system"
ROLE_ADMIN = "admin"
ALL_ROLES = (ROLE_OPERATOR, ROLE_APPROVER, ROLE_BUYER, ROLE_SYSTEM, ROLE_ADMIN)

# ---------------------------------------------------------------- 补货计划
PLAN_DRAFT = "draft"
PLAN_PENDING_APPROVAL = "pending_approval"
PLAN_APPROVED = "approved"
PLAN_REJECTED = "rejected"
PLAN_SUPERSEDED = "superseded"

PLAN_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PLAN_DRAFT: (PLAN_PENDING_APPROVAL,),
    PLAN_PENDING_APPROVAL: (PLAN_APPROVED, PLAN_REJECTED, PLAN_SUPERSEDED),
    PLAN_APPROVED: (PLAN_SUPERSEDED,),  # 仅当尚未创建任何采购单明细
    PLAN_REJECTED: (),
    PLAN_SUPERSEDED: (),
}

# 计划明细标记
LINE_VALID = "valid"
LINE_EXCLUDED = "excluded"
LINE_BLOCKED = "blocked"
LINE_FLAGS = (LINE_VALID, LINE_EXCLUDED, LINE_BLOCKED)

# ---------------------------------------------------------------- 采购单
PO_CREATED = "po_created"
PO_ORDERING = "ordering"
PO_ORDERED = "ordered"
PO_ORDER_UNKNOWN = "order_unknown"
PO_PARTIALLY_RECEIVED = "partially_received"
PO_RECEIVED = "received"
PO_CLOSED = "closed"
PO_CANCELLED = "cancelled"

# 允许转换表（需求 6.2 / 架构 7.2）
PO_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PO_CREATED: (PO_ORDERING, PO_CANCELLED),
    PO_ORDERING: (PO_ORDERED, PO_CREATED, PO_ORDER_UNKNOWN),
    PO_ORDER_UNKNOWN: (PO_ORDERED, PO_CREATED),
    PO_ORDERED: (PO_PARTIALLY_RECEIVED, PO_RECEIVED),
    PO_PARTIALLY_RECEIVED: (PO_PARTIALLY_RECEIVED, PO_RECEIVED),
    PO_RECEIVED: (PO_CLOSED,),
    PO_CLOSED: (),
    PO_CANCELLED: (),
}

# 计入有效在途量的采购单状态（需求 5.3）
INBOUND_PO_STATES = (PO_CREATED, PO_ORDERING, PO_ORDERED, PO_ORDER_UNKNOWN, PO_PARTIALLY_RECEIVED)

# 下单尝试状态
ATTEMPT_ATTEMPTING = "attempting"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"
ATTEMPT_UNKNOWN = "unknown"
ATTEMPT_QUERIED_CREATED = "queried_created"
ATTEMPT_QUERIED_NOT_FOUND = "queried_not_found"

# ---------------------------------------------------------------- 幂等操作
OP_IN_PROGRESS = "in_progress"
OP_RETRYABLE = "retryable"
OP_SUCCEEDED = "succeeded"
OP_FAILED = "failed"
OP_UNKNOWN = "unknown"
OP_STATES = (OP_IN_PROGRESS, OP_RETRYABLE, OP_SUCCEEDED, OP_FAILED, OP_UNKNOWN)

# ---------------------------------------------------------------- 定时执行
EXECUTION_RUNNING = "running"
EXECUTION_SUCCEEDED = "succeeded"
EXECUTION_FAILED = "failed"
EXECUTION_PARTIAL = "partial"

EXEC_ITEM_SUCCESS = "success"
EXEC_ITEM_BLOCKED = "blocked"
EXEC_ITEM_FAILED = "failed"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_RERUN = "rerun"

# ---------------------------------------------------------------- 告警
ALERT_OPEN = "open"
ALERT_CLOSED = "closed"
ALERT_TYPES = (ALERT_OPEN, ALERT_CLOSED)

# ---------------------------------------------------------------- 稳定错误码（HTTP 409/403/422 明细）
ERR_FORBIDDEN = "FORBIDDEN"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_VALIDATION = "VALIDATION_ERROR"
ERR_VERSION_CONFLICT = "VERSION_CONFLICT"
ERR_INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
ERR_PLAN_STALE = "PLAN_STALE"
ERR_ACTIVE_REPLENISHMENT_EXISTS = "ACTIVE_REPLENISHMENT_EXISTS"
ERR_IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
ERR_OPERATION_IN_PROGRESS = "OPERATION_IN_PROGRESS"
ERR_RESOURCE_BUSY = "RESOURCE_BUSY"
ERR_RECEIPT_EVENT_REUSED = "RECEIPT_EVENT_REUSED"
ERR_RECEIPT_EXCEEDS_REMAINING = "RECEIPT_EXCEEDS_REMAINING"
ERR_BLOCKED = "BLOCKED"
ERR_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
ERR_EXTERNAL_UNKNOWN = "EXTERNAL_UNKNOWN"
ERR_CONFLICT = "CONFLICT"
ERR_INTERNAL = "INTERNAL_ERROR"
ERR_LLM_UNAVAILABLE = "LLM_UNAVAILABLE"

# ---------------------------------------------------------------- 数值与哈希规范（需求 5.3.1）
HASH_SCHEMA_VERSION = "stockmind-hash-v1"
NUMERIC_PRECISION = 28  # 十进制有效精度
NUMERIC_SCALE = 12  # 持久化小数位
DISPLAY_SCALE = 6  # 页面与评测展示位
MONEY_SCALE = 2  # 金额小数位（CNY）
HASH_ALGORITHM = "sha256"

# 预测算法 id（固定排序 wma_28_v1 < ses_a03_v1）
ALGO_WMA = "wma_28_v1"
ALGO_SES = "ses_a03_v1"
ALGO_ORDER = (ALGO_WMA, ALGO_SES)

# 预测降级
FALLBACK_FIXED_SAFETY_STOCK = "fixed_safety_stock_fallback"

# 规划窗口白名单（需求 3.1）
ALLOWED_PLANNING_WINDOWS = (7, 14, 30)

# 规则适用优先级（需求 4.3）：SKU > 分类 > 仓库 > 全局
RULE_SCOPE_PRODUCT = "product"
RULE_SCOPE_CATEGORY = "category"
RULE_SCOPE_WAREHOUSE = "warehouse"
RULE_SCOPE_GLOBAL = "global"
RULE_SCOPE_ORDER = (
    RULE_SCOPE_PRODUCT,
    RULE_SCOPE_CATEGORY,
    RULE_SCOPE_WAREHOUSE,
    RULE_SCOPE_GLOBAL,
)

# 规则类型
RULE_SAFETY_STOCK = "safety_stock"
RULE_REPLENISHMENT_POLICY = "replenishment_policy"
RULE_WAREHOUSE_SPECIAL = "warehouse_special"
RULE_SUPPLIER_CONSTRAINT = "supplier_constraint"
RULE_RECEIVING_SOP = "receiving_sop"
RULE_TYPES = (
    RULE_SAFETY_STOCK,
    RULE_REPLENISHMENT_POLICY,
    RULE_WAREHOUSE_SPECIAL,
    RULE_SUPPLIER_CONSTRAINT,
    RULE_RECEIVING_SOP,
)

# 知识文档类型（需求 4.3）
DOC_REPLENISHMENT_POLICY = "replenishment_policy"
DOC_SAFETY_STOCK = "safety_stock"
DOC_WAREHOUSE_SPECIAL = "warehouse_rule"
DOC_SUPPLIER_CONSTRAINT = "supplier_constraint"
DOC_RECEIVING_SOP = "receiving_sop"
DOC_TYPES = (
    DOC_REPLENISHMENT_POLICY,
    DOC_SAFETY_STOCK,
    DOC_WAREHOUSE_SPECIAL,
    DOC_SUPPLIER_CONSTRAINT,
    DOC_RECEIVING_SOP,
)

# 命令类型（幂等作用域）
CMD_DRAFT = "draft_replenishment"
CMD_APPROVE = "approve_plan"
CMD_CREATE_PO = "create_purchase_order"
CMD_PLACE_ORDER = "place_order"
CMD_QUERY_ORDER = "query_order"
CMD_RECEIVE = "receive_goods"
CMD_CLOSE_PO = "close_po"
CMD_CANCEL_PO = "cancel_po"
CMD_SCAN = "scheduled_scan"
CMD_SCHEDULE_UPDATE = "schedule_update"
CMD_SCHEDULE_RUN = "schedule_run"
CMD_FAULT_MODE = "set_fault_mode"
CMD_SUPERSEDE = "supersede_plan"
