"""合成种子数据生成器与 CLI（需求 4.2 / 架构 4）。

固定随机种子生成虚构仓库、SKU、供应商、供货关系、规则与 90-180 天历史出库，
并植入库存充足、在途抵扣、无供应商、规则冲突、交期异常、数据不足/覆盖缺失、
全零验证集等场景。数据可一键销毁并重建；不声称代表真实经营结果。
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.constants import (
    HASH_SCHEMA_VERSION,
    LINE_VALID,
    PLAN_APPROVED,
    RULE_REPLENISHMENT_POLICY,
    RULE_SAFETY_STOCK,
)
from app.db import Base, get_session_factory
from app.models.governance import Schedule, User
from app.models.inventory import DemandDayRecord, Location, Lot, Product, Quant, Warehouse
from app.models.knowledge import Chunk, Document, RuleSource
from app.models.purchasing import (
    PurchaseOrder,
    PurchaseOrderAttempt,
    PurchaseOrderLine,
    ReceiptEvent,
    Supplier,
    SupplierProduct,
)
from app.models.replenishment import PlanLine, ReplenishmentPlan, ReplenishmentRule
from app.seed.documents import DOCS
from app.services.hashing import decision_input_hash

SEED = 20260831

# ---------------------------------------------------------------- 静态主体数据

WAREHOUSES = [
    {"id": "WH-E", "name": "华东仓", "timezone": "Asia/Shanghai"},
    {"id": "WH-S", "name": "华南仓", "timezone": "Asia/Shanghai"},
]

PRODUCTS: list[dict] = [
    # id, 名称, 分类, 基本单位, 场景标签
    {
        "id": "SKU-E01",
        "name": "六角螺栓 M6x20",
        "category": "紧固件",
        "unit": "件",
        "scenario": "正常补货",
    },
    {
        "id": "SKU-E02",
        "name": "六角螺栓 M8x30",
        "category": "紧固件",
        "unit": "件",
        "scenario": "库存充足",
    },
    {
        "id": "SKU-E03",
        "name": "自攻螺丝 ST4.2x16",
        "category": "紧固件",
        "unit": "件",
        "scenario": "在途抵扣",
    },
    {
        "id": "SKU-E04",
        "name": "膨胀螺栓 M10",
        "category": "紧固件",
        "unit": "件",
        "scenario": "多供应商",
    },
    {
        "id": "SKU-E05",
        "name": "尼龙扎带 200mm",
        "category": "包装材料",
        "unit": "包",
        "scenario": "无供应商",
    },
    {
        "id": "SKU-E06",
        "name": "缠绕膜 50cm",
        "category": "包装材料",
        "unit": "卷",
        "scenario": "规则冲突",
    },
    {
        "id": "SKU-E07",
        "name": "瓦楞纸箱 5层",
        "category": "定制包装",
        "unit": "个",
        "scenario": "无规则(华南仓)",
    },
    {
        "id": "SKU-E08",
        "name": "环氧树脂胶 AB 组",
        "category": "化学品",
        "unit": "组",
        "scenario": "预测WMA更优",
    },
    {
        "id": "SKU-E09",
        "name": "润滑油 1L",
        "category": "化学品",
        "unit": "桶",
        "scenario": "预测SES更优",
    },
    {
        "id": "SKU-E10",
        "name": "稀释剂 500ml",
        "category": "化学品",
        "unit": "瓶",
        "scenario": "全零验证集",
    },
    {
        "id": "SKU-E11",
        "name": "焊锡丝 0.8mm",
        "category": "电子元件",
        "unit": "卷",
        "scenario": "数据不足",
    },
    {
        "id": "SKU-E12",
        "name": "热缩管 6mm",
        "category": "电子元件",
        "unit": "米",
        "scenario": "覆盖缺失",
    },
    {
        "id": "SKU-E13",
        "name": "电路板保护漆",
        "category": "电子元件",
        "unit": "罐",
        "scenario": "SKU专属规则",
    },
    {
        "id": "SKU-E14",
        "name": "打包带 PP 19mm",
        "category": "包装材料",
        "unit": "卷",
        "scenario": "SKU专属规则",
    },
    {
        "id": "SKU-E15",
        "name": "工业手套 L 号",
        "category": "劳保",
        "unit": "双",
        "scenario": "仓库特殊规则(华东)",
    },
    {
        "id": "SKU-E16",
        "name": "胶带 48mm",
        "category": "包装材料",
        "unit": "卷",
        "scenario": "关系禁用(无可用供应商)",
    },
]

SUPPLIERS = [
    {"id": "SUP-001", "name": "华东紧固件供应", "priority": 10},
    {"id": "SUP-002", "name": "标准件直供", "priority": 30},
    {"id": "SUP-003", "name": "五金批发联盟", "priority": 50},
    {"id": "SUP-004", "name": "包装耗材厂", "priority": 20},
    {"id": "SUP-005", "name": "化工品供应链", "priority": 40},
    {"id": "SUP-006", "name": "电子元件商城", "priority": 60},
]

# (supplier_id, product_id, price, lead_days, min_qty, pack_multiple, business_priority, enabled, effective_from, effective_to)
RELATIONS: list[tuple] = [
    ("SUP-001", "SKU-E01", "1.20", 3, 20, 10, 10, True, "2026-01-01", None),
    ("SUP-002", "SKU-E01", "1.10", 5, 50, 25, 30, True, "2026-01-01", None),
    ("SUP-001", "SKU-E02", "1.30", 3, 20, 10, 10, True, "2026-01-01", None),
    ("SUP-001", "SKU-E03", "0.90", 3, 20, 10, 10, True, "2026-01-01", None),
    ("SUP-001", "SKU-E04", "2.00", 5, 10, 5, 10, True, "2026-01-01", None),
    ("SUP-002", "SKU-E04", "1.50", 2, 10, 5, 10, True, "2026-01-01", None),
    (
        "SUP-003",
        "SKU-E04",
        "1.20",
        2,
        10,
        5,
        10,
        True,
        "2026-01-01",
        None,
    ),  # 优先级/交期相同，价格更低 → 选中
    (
        "SUP-004",
        "SKU-E05",
        "3.00",
        4,
        10,
        5,
        20,
        True,
        "2026-01-01",
        None,
    ),  # E05 有关系但被禁用场景？— 见下
    ("SUP-004", "SKU-E06", "4.50", 4, 10, 5, 20, True, "2026-01-01", None),
    ("SUP-004", "SKU-E07", "2.80", 4, 10, 5, 20, True, "2026-01-01", None),
    ("SUP-005", "SKU-E08", "18.00", 5, 5, 1, 40, True, "2026-01-01", None),
    ("SUP-005", "SKU-E09", "15.00", 5, 5, 1, 40, True, "2026-01-01", None),
    ("SUP-005", "SKU-E10", "6.00", 5, 10, 5, 40, True, "2026-01-01", None),
    ("SUP-006", "SKU-E11", "25.00", 7, 5, 1, 60, True, "2026-01-01", None),
    ("SUP-006", "SKU-E12", "1.80", 7, 10, 5, 60, True, "2026-01-01", None),
    ("SUP-006", "SKU-E13", "32.00", 7, 5, 1, 60, True, "2026-01-01", None),
    (
        "SUP-004",
        "SKU-E14",
        "2.20",
        3,
        50,
        25,
        20,
        True,
        "2026-01-01",
        None,
    ),  # 最小量 50、整箱 25 演示取整
    ("SUP-004", "SKU-E15", "1.60", 4, 10, 5, 20, True, "2026-01-01", None),
    (
        "SUP-004",
        "SKU-E16",
        "2.00",
        3,
        10,
        5,
        20,
        False,
        "2026-01-01",
        None,
    ),  # 关系被禁用 -> 无可用供应商 -> blocked
]

# E05 无供应商：不为其创建任何关系
RELATIONS = [r for r in RELATIONS if r[1] != "SKU-E05"]

# 结构化规则（rule_type, scope, 范围key, safety_stock, review, version, source_doc, source_chunk_seq）
RULES: list[dict] = [
    # 仓库规则（华东仓）
    {
        "code": "SS-WHE-01",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "warehouse",
        "scope_warehouse_id": "WH-E",
        "safety_stock": 50,
        "review": 5,
        "version": 1,
        "doc": "doc-wh-01",
        "chunk": 1,
        "name": "华东仓安全库存",
        "scope_name": "WH-E",
    },
    {
        "code": "RP-WHE-01",
        "rule_type": RULE_REPLENISHMENT_POLICY,
        "scope": "warehouse",
        "scope_warehouse_id": "WH-E",
        "safety_stock": None,
        "review": 5,
        "version": 1,
        "doc": "doc-wh-01",
        "chunk": 1,
        "name": "华东仓复查周期",
        "scope_name": "WH-E",
    },
    # 分类规则
    {
        "code": "SS-CAT-FASTEN",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "category",
        "scope_category": "紧固件",
        "safety_stock": 30,
        "review": 3,
        "version": 1,
        "doc": "doc-ss-01",
        "chunk": 2,
        "name": "紧固件安全库存",
        "scope_name": "紧固件",
    },
    {
        "code": "SS-CAT-PACK",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "category",
        "scope_category": "包装材料",
        "safety_stock": 25,
        "review": 3,
        "version": 1,
        "doc": "doc-ss-01",
        "chunk": 2,
        "name": "包装材料安全库存",
        "scope_name": "包装材料",
    },
    {
        "code": "SS-CAT-CHEM",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "category",
        "scope_category": "化学品",
        "safety_stock": 40,
        "review": 4,
        "version": 1,
        "doc": "doc-ss-01",
        "chunk": 2,
        "name": "化学品安全库存",
        "scope_name": "化学品",
    },
    {
        "code": "SS-CAT-ELEC",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "category",
        "scope_category": "电子元件",
        "safety_stock": 35,
        "review": 3,
        "version": 1,
        "doc": "doc-ss-01",
        "chunk": 2,
        "name": "电子元件安全库存",
        "scope_name": "电子元件",
    },
    # SKU 专属规则
    {
        "code": "SS-SKU-E13",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "product",
        "scope_product_id": "SKU-E13",
        "safety_stock": 60,
        "review": 7,
        "version": 1,
        "doc": "doc-ss-01",
        "chunk": 3,
        "name": "SKU-E13 专属安全库存",
        "scope_name": "SKU-E13",
    },
    {
        "code": "SS-SKU-E14",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "product",
        "scope_product_id": "SKU-E14",
        "safety_stock": 100,
        "review": 3,
        "version": 1,
        "doc": "doc-ss-01",
        "chunk": 3,
        "name": "SKU-E14 专属安全库存",
        "scope_name": "SKU-E14",
    },
    # 规则冲突场景：同一 SKU 两条产品级规则（版本相同、均启用、均有效）
    {
        "code": "SS-SKU-E06-A",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "product",
        "scope_product_id": "SKU-E06",
        "safety_stock": 10,
        "review": 3,
        "version": 2,
        "doc": "doc-ss-01",
        "chunk": 4,
        "name": "SKU-E06 规则A(冲突)",
        "scope_name": "SKU-E06",
    },
    {
        "code": "SS-SKU-E06-B",
        "rule_type": RULE_SAFETY_STOCK,
        "scope": "product",
        "scope_product_id": "SKU-E06",
        "safety_stock": 90,
        "review": 3,
        "version": 2,
        "doc": "doc-ss-01",
        "chunk": 4,
        "name": "SKU-E06 规则B(冲突)",
        "scope_name": "SKU-E06",
    },
    # 全局复查周期（无全局安全库存，保证"无规则→阻断"场景可达）
    {
        "code": "RP-GLOBAL-01",
        "rule_type": RULE_REPLENISHMENT_POLICY,
        "scope": "global",
        "safety_stock": None,
        "review": 2,
        "version": 1,
        "doc": "doc-rp-01",
        "chunk": 2,
        "name": "全局复查周期",
        "scope_name": None,
    },
]

# 每 SKU 每仓库初始库存 (on_hand, reserved)
INITIAL_QUANT: dict[str, dict[str, tuple[int, int]]] = {
    "SKU-E01": {"WH-E": (12, 2), "WH-S": (10, 1)},
    "SKU-E02": {"WH-E": (600, 10), "WH-S": (400, 5)},
    "SKU-E03": {"WH-E": (30, 5), "WH-S": (20, 2)},
    "SKU-E04": {"WH-E": (8, 1), "WH-S": (6, 0)},
    "SKU-E05": {"WH-E": (15, 0), "WH-S": (12, 0)},
    "SKU-E06": {"WH-E": (20, 2), "WH-S": (18, 1)},
    "SKU-E07": {"WH-E": (10, 0), "WH-S": (8, 0)},
    "SKU-E08": {"WH-E": (5, 0), "WH-S": (4, 0)},
    "SKU-E09": {"WH-E": (10, 0), "WH-S": (8, 0)},
    "SKU-E10": {"WH-E": (4, 0), "WH-S": (3, 0)},
    "SKU-E11": {"WH-E": (6, 0), "WH-S": (5, 0)},
    "SKU-E12": {"WH-E": (8, 0), "WH-S": (6, 0)},
    "SKU-E13": {"WH-E": (9, 1), "WH-S": (7, 0)},
    "SKU-E14": {"WH-E": (25, 5), "WH-S": (20, 3)},
    "SKU-E15": {"WH-E": (3, 0), "WH-S": (3, 0)},
    "SKU-E16": {"WH-E": (7, 0), "WH-S": (6, 0)},
}

USERS = [
    {"id": "alice", "name": "演示管理员", "roles": ["operator", "approver", "buyer", "admin"]},
    {"id": "bob", "name": "操作员小博", "roles": ["operator"]},
    {"id": "carol", "name": "审批员小卡", "roles": ["approver"]},
    {"id": "dave", "name": "采购员小戴", "roles": ["buyer"]},
    {"id": "eve", "name": "管理员小伊", "roles": ["admin"]},
    {"id": "system", "name": "内部服务主体", "roles": ["system"]},
]


# ---------------------------------------------------------------- 需求模式


def demand_pattern(product_id: str, rng: random.Random):
    """返回 (days_count, fn(idx_from_end)->demand, incomplete_days)。

    idx_from_end=0 表示业务日（昨天）。
    """
    if product_id == "SKU-E02":
        return 120, lambda _: max(0, 6 + rng.randint(-2, 2)), set()
    if product_id == "SKU-E08":
        # 线性趋势（越近越高）：WMA 加权窗口追踪趋势
        n = 150

        def fn(i: int) -> int:
            return max(1, 5 + (n - 1 - i) // 2)

        return n, fn, set()
    if product_id == "SKU-E09":
        # 近期水平跃迁（最近 14 天需求 30，此前 5）：SES 更快收敛
        n = 150

        def fn(i: int) -> int:
            return 30 if i < 14 else 5

        return n, fn, set()
    if product_id == "SKU-E10":
        # 全零验证集：最近 ≥56 日全部为 0
        n = 120
        return n, lambda _: 0, set()
    if product_id == "SKU-E11":
        # 数据不足：仅 30 日
        n = 30
        return n, lambda _: max(0, 8 + rng.randint(-3, 3)), set()
    if product_id == "SKU-E12":
        # 覆盖缺失：最近 56 日内存在来源不完整日期
        n = 120
        incomplete = {10, 28, 45}
        return n, lambda _: max(0, 7 + rng.randint(-2, 2)), incomplete
    # 其余：平稳波动
    base = {
        "SKU-E01": 9,
        "SKU-E03": 12,
        "SKU-E04": 7,
        "SKU-E05": 8,
        "SKU-E06": 10,
        "SKU-E07": 5,
        "SKU-E13": 11,
        "SKU-E14": 20,
        "SKU-E15": 4,
        "SKU-E16": 6,
    }[product_id]
    noise = 3 if base >= 10 else 2
    n = rng.randint(90, 120)
    return n, lambda _: max(0, base + rng.randint(-noise, noise)), set()


# ---------------------------------------------------------------- 写入逻辑


def _seed_warehouses_products(session: Session) -> None:
    # 显式按 FK 依赖顺序 flush，避免依赖排序依赖 SQLAlchemy 内部行为
    for w in WAREHOUSES:
        session.add(Warehouse(id=w["id"], name=w["name"], timezone=w["timezone"]))
    session.flush()
    for p in PRODUCTS:
        session.add(Product(id=p["id"], name=p["name"], category=p["category"], basic_unit=p["unit"]))
    session.flush()
    for w in WAREHOUSES:
        session.add(Location(id=f"LOC-{w['id']}", warehouse_id=w["id"], name="主库区"))
    session.flush()


def _seed_suppliers_relations(session: Session, business_date: date) -> None:
    for s in SUPPLIERS:
        session.add(Supplier(id=s["id"], name=s["name"], business_priority=s["priority"], currency="CNY"))
    session.flush()
    for sup, pid, price, lead, min_qty, pack, prio, enabled, frm, to in RELATIONS:
        session.add(
            SupplierProduct(
                supplier_id=sup,
                product_id=pid,
                business_priority=prio,
                price=Decimal(price),
                lead_days=lead,
                minimum_order_qty=min_qty,
                pack_multiple=pack,
                effective_from=date.fromisoformat(frm),
                effective_to=date.fromisoformat(to) if to else None,
                enabled=enabled,
            )
        )
    session.flush()


def _seed_documents(session: Session) -> None:
    from app.rag.fts import to_search_text

    for doc in DOCS:
        d = Document(
            id=doc["id"],
            title=doc["title"],
            doc_type=doc["doc_type"],
            version=doc["version"],
            effective_from=date.fromisoformat(doc["effective_from"]),
            effective_to=None,
            enabled=doc["enabled"],
            content_text="\n".join(doc["chunks"]),
        )
        session.add(d)
        session.flush()
        for idx, chunk_text in enumerate(doc["chunks"], start=1):
            session.add(
                Chunk(
                    id=f"{doc['id']}-c{idx}",
                    document_id=doc["id"],
                    seq_no=idx,
                    content=chunk_text,
                    search_text=to_search_text(chunk_text),
                )
            )


def _seed_rules(session: Session) -> None:
    for r in RULES:
        source_chunk = f"{r['doc']}-c{r['chunk']}"
        rule = ReplenishmentRule(
            code=r["code"],
            name=r["name"],
            rule_type=r["rule_type"],
            scope=r["scope"],
            scope_product_id=r.get("scope_product_id"),
            scope_category=r.get("scope_category"),
            scope_warehouse_id=r.get("scope_warehouse_id"),
            safety_stock=Decimal(str(r["safety_stock"])) if r["safety_stock"] is not None else None,
            review_period_days=r["review"],
            params={"review_period_days": r["review"]},
            version=r["version"],
            effective_from=date(2026, 1, 1),
            effective_to=None,
            enabled=True,
            source_document_id=r["doc"],
            source_chunk_id=source_chunk,
        )
        session.add(rule)
        session.flush()
        session.add(
            RuleSource(
                rule_id=rule.id,
                source_document_id=r["doc"],
                source_chunk_id=source_chunk,
                extracted_params={
                    "safety_stock": r["safety_stock"],
                    "review_period_days": r["review"],
                },
            )
        )


def _seed_demand(session: Session, business_date: date) -> None:
    rng = random.Random(SEED)
    for p in PRODUCTS:
        pid = p["id"]
        days, fn, incomplete = demand_pattern(pid, rng)
        for wh in WAREHOUSES:
            wid = wh["id"]
            for idx in range(days):
                day = business_date - timedelta(days=idx)
                complete = idx not in incomplete
                session.add(
                    DemandDayRecord(
                        warehouse_id=wid,
                        product_id=pid,
                        day=day,
                        demand=fn(idx),
                        source_complete=complete,
                    )
                )


def _seed_quants(session: Session, business_date: date) -> None:
    for p in PRODUCTS:
        pid = p["id"]
        for wh in WAREHOUSES:
            wid = wh["id"]
            on_hand, reserved = INITIAL_QUANT[pid][wid]
            session.add(
                Lot(
                    id=f"LOT-{wid}-{pid}",
                    product_id=pid,
                    location_id=f"LOC-{wid}",
                    batch_no=f"B{pid}-A1",
                )
            )
    session.flush()  # Lot 依赖 Location
    for p in PRODUCTS:
        pid = p["id"]
        for wh in WAREHOUSES:
            wid = wh["id"]
            on_hand, reserved = INITIAL_QUANT[pid][wh["id"]]
            session.add(
                Quant(
                    warehouse_id=wid,
                    product_id=pid,
                    location_id=f"LOC-{wid}",
                    lot_id=f"LOT-{wid}-{pid}",
                    on_hand=on_hand,
                    reserved=reserved,
                )
            )
    session.flush()  # Quant 依赖 Lot


def _seed_users(session: Session) -> None:
    for u in USERS:
        session.add(User(id=u["id"], display_name=u["name"], roles=u["roles"]))
    # 演示用默认定时任务（最低周期 1 分钟，生产建议每日）
    session.add(
        Schedule(
            name="每日低库存扫描",
            cron_expr="*/1 * * * *",
            timezone="Asia/Shanghai",
            enabled=True,
            default_window=14,
            created_by="admin",
        )
    )


def _seed_inbound_demo(session: Session, business_date: date) -> None:
    """在途抵扣场景（SKU-E03/华东仓）：3 张已批准计划的采购单处于不同状态。"""
    product_id = "SKU-E03"
    wid = "WH-E"
    on_hand, reserved = INITIAL_QUANT[product_id][wid]
    # 三个历史计划（均已批准并建单，active_for_dedupe 已释放）
    po_specs: list[dict[str, object]] = [
        {"status": "po_created", "order_qty": 100, "received": 0, "attempt": None},
        {"status": "ordered", "order_qty": 50, "received": 0, "attempt": "succeeded"},
        {"status": "partially_received", "order_qty": 80, "received": 30, "attempt": "succeeded"},
    ]
    for i, spec in enumerate(po_specs, start=1):
        plan = ReplenishmentPlan(
            id=f"SEED-PLAN-{i}",
            warehouse_id=wid,
            thread_id=None,
            actor_id="system",
            trigger_type="scheduled",
            status=PLAN_APPROVED,
            requested_window=14,
            planning_date=business_date,
            decision_version=1,
        )
        session.add(plan)
        session.flush()
        hash_inputs = {
            "warehouse_id": wid,
            "product_id": product_id,
            "business_date": business_date.isoformat(),
            "requested_window": 14,
            "on_hand": on_hand,
            "reserved": reserved,
            "quant_version": 1,
            "inbound": [],
            "demand_cutoff_date": business_date.isoformat(),
            "rules": [{"rule_id": "SS-CAT-FASTEN", "version": 1}],
            "supplier": {"supplier_id": "SUP-001", "relation_id": "seed", "version": 1},
            "algorithm_version": "stockmind-replenish-v1",
        }
        line = PlanLine(
            plan_id=plan.id,
            warehouse_id=wid,
            product_id=product_id,
            flag=LINE_VALID,
            active_for_dedupe=False,
            planning_window=14,
            coverage_demand=Decimal("168"),
            target_stock=Decimal("198"),
            available=Decimal(on_hand - reserved),
            inbound=Decimal("0"),
            net_demand=Decimal("170"),
            order_qty=spec["order_qty"],
            fixed_safety_stock=Decimal("30"),
            lead_days=3,
            review_period_days=3,
            minimum_order_qty=20,
            pack_multiple=10,
            supplier_id="SUP-001",
            on_hand=on_hand,
            reserved=reserved,
            decision_input_hash=decision_input_hash(hash_inputs),
            hash_schema_version=HASH_SCHEMA_VERSION,
            input_snapshot=hash_inputs,
            intermediate={},
            rule_refs=hash_inputs["rules"],
            algorithm_version="stockmind-replenish-v1",
            demand_cutoff_date=business_date,
            quant_version=1,
            supplier_rel_version=1,
        )
        session.add(line)
        session.flush()
        po = PurchaseOrder(
            id=f"SEED-PO-{i}",
            warehouse_id=wid,
            plan_id=plan.id,
            supplier_id="SUP-001",
            status=spec["status"],
        )
        session.add(po)
        session.flush()
        pol = PurchaseOrderLine(
            purchase_order_id=po.id,
            plan_line_id=line.id,
            product_id=product_id,
            order_qty=spec["order_qty"],
            received_qty=spec["received"],
            unit_price=Decimal("0.90"),
        )
        session.add(pol)
        session.flush()
        if spec["attempt"] == "succeeded":
            session.add(
                PurchaseOrderAttempt(
                    purchase_order_id=po.id,
                    attempt_no=1,
                    internal_idempotency_key=f"seed-{i}",
                    supplier_idempotency_key=f"po:{po.id}:att:1",
                    request_hash="seed",
                    hash_schema_version=HASH_SCHEMA_VERSION,
                    status="succeeded",
                    external_order_no=f"EXT-SEED-{i}",
                    response_summary="seeded",
                )
            )
        received = spec["received"]
        if isinstance(received, int) and received > 0:
            session.add(
                ReceiptEvent(
                    receipt_event_id=f"SEED-RCPT-{i}",
                    purchase_order_line_id=pol.id,
                    warehouse_id=wid,
                    product_id=product_id,
                    qty=spec["received"],
                    payload_hash="seed",
                    hash_schema_version=HASH_SCHEMA_VERSION,
                    actor_id="system",
                )
            )


# ---------------------------------------------------------------- CLI


def run_seed(session: Session, *, reset: bool = True) -> dict:
    engine = session.get_bind()  # noqa: F841

    if reset:
        session.execute(text("DROP SCHEMA public CASCADE"))
        session.execute(text("CREATE SCHEMA public"))
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))  # DROP SCHEMA 会移除扩展
        session.commit()
        Base.metadata.create_all(engine)
        _recreate_checkpoint_tables()
        # DROP SCHEMA 会移除 alembic_version；重建后标记为 head，保证后续
        # `alembic upgrade head` 幂等（否则 api 重启时重复建表报 DuplicateTable）。
        _stamp_alembic_head()

    # 业务日：按种子仓库时区（Asia/Shanghai）取昨日
    from zoneinfo import ZoneInfo

    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    business_date = (now_local - timedelta(days=1)).date()

    _seed_warehouses_products(session)
    _seed_suppliers_relations(session, business_date)
    _seed_documents(session)
    _seed_rules(session)
    _seed_demand(session, business_date)
    _seed_quants(session, business_date)
    _seed_users(session)
    _seed_inbound_demo(session, business_date)
    session.commit()

    return {
        "business_date": business_date.isoformat(),
        "warehouses": len(WAREHOUSES),
        "products": len(PRODUCTS),
        "suppliers": len(SUPPLIERS),
        "rules": len(RULES),
        "users": len(USERS),
    }


def _recreate_checkpoint_tables() -> None:
    """重置 schema 后重建 LangGraph checkpoint 表（生产使用 PostgreSQL checkpointer）。"""
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        from app.config import get_settings

        dsn = get_settings().postgres_dsn.replace("postgresql+psycopg://", "postgresql://")
        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        saver = PostgresSaver(conn)
        saver.setup()
        conn.close()
    except Exception:  # noqa: BLE001  内存 checkpointer 或不可用时跳过
        pass


def _stamp_alembic_head() -> None:
    """把 alembic_version 标记为当前 head，保证 alembic upgrade 幂等。"""
    import logging

    try:
        from alembic.config import Config

        from alembic import command

        command.stamp(Config("alembic.ini"), "head")
    except Exception as exc:  # noqa: BLE001  迁移不可用/环境不同时仅告警
        logging.getLogger("stockmind.seed").warning("alembic stamp head 失败（不影响种子数据）: %s", exc)


def seed_if_empty(session: Session) -> dict | None:
    """仅在业务库为空时初始化种子；已有业务状态必须保持不变。"""
    if session.scalar(select(Warehouse.id).limit(1)) is not None:
        return None
    return run_seed(session, reset=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StockMind 合成种子数据（可一键重建）")
    parser.add_argument("--reset", action="store_true", help="先销毁并重建全部数据")
    parser.add_argument("--with-embeddings", action="store_true", help="同时生成 RAG 向量（需联网下载模型）")
    args = parser.parse_args(argv)

    factory = get_session_factory()
    with factory() as session:
        # 默认启动只在空库初始化，避免 API/Worker 重启清空已有业务状态。
        # 需要重建演示数据时必须显式传 --reset。
        summary = run_seed(session, reset=True) if args.reset else seed_if_empty(session)
        if summary is None:
            print("检测到已有业务数据，跳过种子重建（如需清空重建请显式使用 --reset）")
    if summary is not None:
        print(f"种子完成：{summary}")

    if args.with_embeddings:
        from app.rag.embedding import index_all_embeddings

        with factory() as session:
            index_all_embeddings(session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
