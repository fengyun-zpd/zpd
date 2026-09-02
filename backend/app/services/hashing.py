"""canonical JSON + SHA-256 哈希规范（需求 5.3.1 / 架构 6.3）。

规范：UTF-8 canonical JSON —— 对象键按字典序；集合按稳定业务 id 排序；
日期/时间用 ISO 8601 且带明确时区；十进制数用无指数规范字符串；null 显式保留。
哈希算法 SHA-256；schema 版本随计划、幂等记录与下单尝试保存，历史哈希不可重写。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.constants import HASH_SCHEMA_VERSION


def canonical_decimal(value: Decimal) -> str:
    """无指数规范字符串：先 normalize 消除尾零，再用定点格式输出。

    Decimal('12.50') -> '12.5'; Decimal('0E-12') -> '0'; 大数不会出现指数。
    """
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"NaN/Inf 不允许进入业务哈希: {value}")
    try:
        normalized = value.normalize()
    except InvalidOperation:  # pragma: no cover
        normalized = value
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def canonical_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"业务哈希要求带时区的 datetime，收到 naive: {value!r}")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        raise ValueError("业务载荷不允许二进制浮点进入哈希；请使用 Decimal/整数（需求 5.3.1）")
    raise TypeError(f"不支持的类型进入业务哈希: {type(value)!r}")


def canonical_json(value: Any) -> str:
    """递归生成 canonical JSON 字符串（UTF-8 文本）。"""

    def emit(v: Any) -> str:
        if isinstance(v, dict):
            items = sorted(v.items(), key=lambda kv: kv[0])
            body = ",".join(f"{canonical_scalar(k)}:{emit(val)}" for k, val in items)
            return "{" + body + "}"
        if isinstance(v, (list, tuple)):
            return "[" + ",".join(emit(item) for item in v) + "]"
        if isinstance(v, (set, frozenset)):
            # 集合按稳定业务 id（字符串表示）排序
            ordered = sorted(v, key=lambda item: canonical_scalar(item))
            return "[" + ",".join(emit(item) for item in ordered) + "]"
        return canonical_scalar(v)

    return emit(value)


def payload_hash(payload: Any, schema_version: str = HASH_SCHEMA_VERSION) -> str:
    """带 schema_version 的载荷哈希（SHA-256 hex）。"""
    envelope = {"schema_version": schema_version, "payload": payload}
    text = canonical_json(envelope)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decision_input_hash(inputs: dict[str, Any], schema_version: str = HASH_SCHEMA_VERSION) -> str:
    """decision_input_hash：对已规范化的决策输入生成 SHA-256。"""
    return payload_hash(inputs, schema_version)
