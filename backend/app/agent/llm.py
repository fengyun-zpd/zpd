"""可选 LLM 路径（OpenAI 兼容接口；V1 验证 DeepSeek）。

仅当配置 LLM_API_KEY 时启用；失败自动回退离线模式。未配置 Key 时不调用外部服务。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from app.config import get_settings

logger = logging.getLogger("stockmind.agent.llm")


def _chat(messages: list[dict], *, temperature: float = 0.0) -> str:
    settings = get_settings()
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    llm = ChatOpenAI(
        base_url=settings.llm_base_url,
        api_key=SecretStr(settings.llm_api_key),
        model=settings.llm_model,
        temperature=temperature,
        timeout=settings.llm_timeout_seconds,
    )
    from app.observability import record_generation

    start = time.time()
    try:
        resp = llm.invoke(messages)
        text = str(resp.content)
        usage = getattr(resp, "usage_metadata", None) or {}
        record_generation(
            name="llm.chat",
            model=settings.llm_model,
            input_=messages,
            output=text,
            usage={
                "input": usage.get("input_tokens"),
                "output": usage.get("output_tokens"),
                "total": usage.get("total_tokens"),
            },
            start_time=datetime.fromtimestamp(start, tz=timezone.utc),
            end_time=datetime.fromtimestamp(time.time(), tz=timezone.utc),
            level="DEFAULT",
        )
        return text
    except Exception as exc:  # noqa: BLE001 LLM 失败也要记录观测（不掩盖错误）
        record_generation(
            name="llm.chat",
            model=settings.llm_model,
            input_=messages,
            output=None,
            start_time=datetime.fromtimestamp(start, tz=timezone.utc),
            end_time=datetime.fromtimestamp(time.time(), tz=timezone.utc),
            level="ERROR",
            status_message=str(exc)[:500],
        )
        raise


def llm_parse_params(text: str) -> dict:
    """LLM 解析意图与参数（结构化输出）。

    规则优先级（确定性优先，避免 LLM 歧义）：
    - 消息含"补货/采购/进货/订货/备货/缺货/低库存"等词 => intent 必须为 replenish；
    - "检查/查询/多少/库存量/剩余/在途"且无补货词 => query；
    - "为什么/解释/依据/怎么算/说明" => explain；
    - 商品范围可以是分类名（如"紧固件"），此时 products 填写该分类名，
      由系统按分类展开为多个 SKU；无法确定则 null 并放入 missing。
    """
    prompt = (
        "你是仓储补货助手。从用户消息中解析 JSON，只输出 JSON："
        '{"intent": "replenish|query|explain|other", '
        '"warehouse_id": "WH-E|WH-S|null", '
        '"products": ["SKU-..." 或商品分类名，如 "紧固件"], '
        '"requested_window": 7|14|30|null, "missing": ["字段名"]}。'
        "意图判定规则（必须遵守）：消息包含'补货/采购/进货/订货/备货/缺货/低库存'时 "
        "intent 固定为 replenish；'为什么/解释/依据'优先 explain；其余查询类为 query。"
        "仓库只能填 WH-E 或 WH-S；规划窗口只允许 7/14/30；"
        "无法确定的必填字段填 null 并把字段名放入 missing。"
        "示例：'帮我检查华东仓未来两周需要补货的紧固件' -> "
        '{"intent": "replenish", "warehouse_id": "WH-E", "products": ["紧固件"], '
        '"requested_window": 14, "missing": []}。'
        "消息：" + text
    )
    raw = _chat([{"role": "user", "content": prompt}])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:  # noqa: BLE001
        raise ValueError(f"LLM 返回非 JSON: {raw[:200]}") from exc
    return {
        "intent": data.get("intent", "other"),
        "params": {
            "warehouse_id": data.get("warehouse_id") or None,
            "products": [p for p in (data.get("products") or []) if p],
            "requested_window": data.get("requested_window") or None,
        },
        "missing": data.get("missing", []),
    }
