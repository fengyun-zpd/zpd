"""规则解析器（需求 4.3 / 架构 5）。

把候选分块中的数字确定性解析为结构化规则候选，再经 Pydantic 校验。
运行时 LLM 不得从自然语言临时转出计算参数；只有本解析器的已校验输出可进入计算。
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class StructuredRule(BaseModel):
    rule_type: str
    scope: str
    scope_key: str | None = None
    safety_stock: Decimal | None = Field(default=None, ge=0)
    review_period_days: int | None = Field(default=None, ge=0)
    version: int = Field(default=1, ge=1)
    source_document_id: str
    source_chunk_id: str

    @field_validator("scope")
    @classmethod
    def _scope_enum(cls, v: str) -> str:
        if v not in ("product", "category", "warehouse", "global"):
            raise ValueError(f"非法作用域 {v}")
        return v

    @field_validator("rule_type")
    @classmethod
    def _type_enum(cls, v: str) -> str:
        if v not in (
            "safety_stock",
            "replenishment_policy",
            "warehouse_special",
            "supplier_constraint",
            "receiving_sop",
        ):
            raise ValueError(f"非法规则类型 {v}")
        return v


_SAFETY_RE = re.compile(r"安全库存\s*(?:为|是|＝|:)?\s*(\d+(?:\.\d+)?)\s*件")
_REVIEW_RE = re.compile(r"复查周期\s*(?:为|是|＝|:)?\s*(\d+)\s*天")


def parse_rule_candidates(chunk_text: str, *, source_document_id: str, source_chunk_id: str) -> list[StructuredRule]:
    """从分块文本确定性解析结构化规则候选（仅数字抽取，不做业务猜测）。"""
    candidates: list[StructuredRule] = []
    safety_match = _SAFETY_RE.search(chunk_text)
    review_match = _REVIEW_RE.search(chunk_text)
    if safety_match or review_match:
        candidates.append(
            StructuredRule(
                rule_type="safety_stock",
                scope="global",  # 作用域由调用方结合文档上下文确认
                safety_stock=Decimal(safety_match.group(1)) if safety_match else None,
                review_period_days=int(review_match.group(1)) if review_match else None,
                source_document_id=source_document_id,
                source_chunk_id=source_chunk_id,
            )
        )
    return candidates
