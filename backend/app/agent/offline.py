"""离线演示模式（无 LLM Key 时的确定性意图/参数提取与解释）。

仅用于本机闭环验证；标记 offline=true，不冒充真实 LLM 能力。
LLM 配置后由 llm.py 接管；离线路径保证规则确定性，不依赖外部服务。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.inventory import Product, Warehouse

_WINDOW_PATTERNS = [
    (7, re.compile(r"(7|七)\s*(天|日)|一周|1\s*周")),
    (14, re.compile(r"(14|十四)\s*(天|日)|两周|2\s*周")),
    (30, re.compile(r"(30|三十)\s*(天|日)|一个月|一月|30\s*天")),
]

_WAREHOUSE_ALIASES = {
    "华东": "WH-E",
    "华东仓": "WH-E",
    "WH-E": "WH-E",
    "上海仓": "WH-E",
    "华南": "WH-S",
    "华南仓": "WH-S",
    "WH-S": "WH-S",
    "广州仓": "WH-S",
}

_INTENT_REPLENISH = re.compile(r"补货|采购|进货|订货|备货|库存不足|低库存|缺货|下单")
_INTENT_QUERY = re.compile(r"查|看|多少|库存量|在途|需求|历史|剩余")
_INTENT_EXPLAIN = re.compile(r"为什么|解释|依据|原因|怎么算|公式|说明")

# 预算/成本约束：V2 才参与金额阈值审批（ADR 001 / 架构 V2 行）；V1 中识别但绝不当作必填参数
_BUDGET_RE = re.compile(r"预算|成本|金额|花费|费用|上限|不超过.{0,6}(元|块)|控制在.{0,6}内|万元")

# 缺参业务标签（对外自然语言，禁止在回复中暴露内部字段名）
_MISSING_LABEL = {
    "warehouse": "仓库",
    "product": "SKU 或商品分类",
    "requested_window": "规划周期（7/14/30 天）",
}

_ALLOWED_MISSING = ("warehouse", "product", "requested_window")

# 可直接复用的自然语言句式（前端示例按钮与此一致）
REPLENISH_EXAMPLE = "帮我检查华东仓未来14天需要补货的紧固件"
BUDGET_NOTE = "预算/成本约束暂不参与 V1 补货计算，无需提供（金额阈值审批属于后续版本）。"


def normalize_missing(missing: list[str]) -> list[str]:
    """missing 只保留业务白名单字段；LLM 返回的未知键（如 budget）一律丢弃。"""
    return [m for m in missing if m in _ALLOWED_MISSING]


def missing_label(missing: list[str]) -> str:
    return "、".join(_MISSING_LABEL[m] for m in normalize_missing(missing))


@dataclass
class ParsedIntent:
    intent: str  # replenish / query / explain / other
    params: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    clarification: str | None = None
    budget_note: bool = False  # 用户提到预算/成本约束（V1 不参与计算，仅提示说明）


def classify_intent(text: str) -> str:
    # "为什么补货" 优先解释意图，再匹配补货/查询
    if _INTENT_EXPLAIN.search(text):
        return "explain"
    if _INTENT_REPLENISH.search(text):
        return "replenish"
    if _INTENT_QUERY.search(text):
        return "query"
    return "other"


def _match_window(text: str) -> int | None:
    for window, pattern in _WINDOW_PATTERNS:
        if pattern.search(text):
            return window
    return None


def parse_params(text: str, session: Session) -> ParsedIntent:
    intent = classify_intent(text)
    budget_note = bool(_BUDGET_RE.search(text))
    params: dict = {}
    missing: list[str] = []

    if intent != "replenish":
        return ParsedIntent(intent=intent, params=params, missing=missing, budget_note=budget_note)

    # 仓库
    warehouse_id = None
    for alias, wid in _WAREHOUSE_ALIASES.items():
        if alias in text:
            warehouse_id = wid
            break
    if warehouse_id is None:
        wh = session.scalar(select(Warehouse).order_by(Warehouse.id).limit(1))
        if wh is not None and wh.id in text:
            warehouse_id = wh.id
    if warehouse_id:
        params["warehouse_id"] = warehouse_id
    else:
        missing.append("warehouse")

    # SKU 或商品范围（分类）
    products: list[str] = []
    sku_matches = re.findall(r"SKU-?[A-Z0-9]+", text.upper().replace(" ", ""))
    for sku in sku_matches:
        normalized = sku.replace("SKU-", "SKU-")  # 保持标准形式
        products.append(normalized)
    if not products:
        all_products = session.scalars(select(Product)).all()
        for p in all_products:
            if p.id in text or p.name in text:
                products.append(p.id)
        # 分类范围
        if not products:
            for p in all_products:
                if p.category in text:
                    products.append(p.id)
    if products:
        params["products"] = list(dict.fromkeys(products))
    else:
        missing.append("product")

    # 规划窗口
    window = _match_window(text)
    if window:
        params["requested_window"] = window
    else:
        missing.append("requested_window")

    clarification = None
    if missing:
        clarification = "缺少必要参数：" + "、".join(_MISSING_LABEL[m] for m in missing) + "，请补充。"
        if budget_note:
            clarification += BUDGET_NOTE

    return ParsedIntent(
        intent=intent,
        params=params,
        missing=normalize_missing(missing),
        clarification=clarification,
        budget_note=budget_note,
    )


def build_explanation(plan_id: str, draft: dict | None) -> str:
    """解释草稿结果（离线确定性文案；LLM 模式可替换为生成式解释）。"""
    if draft is None or not draft.get("created"):
        lines = [o for o in (draft or {}).get("lines", []) if o.get("flag") == "blocked"]
        if lines:
            detail = "；".join(f"{o['product_id']}: {o['blocked_reason']}" for o in lines)
            return f"本次没有可提交的补货建议，阻断原因：{detail}"
        return "本次没有可提交的补货建议。"
    valid = [o for o in draft["lines"] if o.get("flag") == "valid"]
    text = "已按「规则校验 → 预测 → 供应商选择 → 补货量计算」生成补货草稿并提交待审批：\n"
    for o in valid:
        text += f"- {o['product_id']}：建议补货 {o['order_qty']} 件（供应商 {o.get('supplier_id')}）\n"
    text += "请审批人进入审批箱逐条批准或排除。"
    return text
