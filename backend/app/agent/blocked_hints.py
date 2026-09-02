"""阻断码到"下一步动作"的展示映射（V1 收口功能优化）。

只做展示辅助：把领域服务返回的 blocked_code / 稳定错误码映射为面向操作员的
下一步建议文案。不改变领域校验、权限边界或状态机（宪法第八、九、十条）。
"""

from __future__ import annotations

# blocked_code / 错误码 -> (标题, 下一步动作)
NEXT_STEP: dict[str, tuple[str, str]] = {
    # 草稿阻断
    "no_rule": ("无适用规则", "请核对仓库/SKU 是否在规则知识库范围内，或联系管理员补全规则。"),
    "rule_conflict": ("规则冲突", "系统无法确定采用哪条规则；请人工核对规则版本与适用范围，排除冲突后重算。"),
    "illegal_input": (
        "输入数据非法",
        "库存、预留量、交期或规则值不合法（如 reserved>on_hand、整箱倍数≤0）；请先修正基础数据。",
    ),
    "no_supplier": ("无可用供应商", "该 SKU 没有有效供应商关系；请先在供应商关系维护中补充。"),
    "data_insufficient": ("数据不足", "历史需求覆盖不完整，无法可靠预测；可按固定安全库存降级或补充数据后重算。"),
    "invalid_rule_type": ("规则类型无效", "规则配置异常；请检查规则类型与版本。"),
    "runtime_error": ("计算异常", "补货计算发生未分类错误；请查看执行记录，联系维护。"),
    # 防重 / 并发
    "ACTIVE_REPLENISHMENT_EXISTS": (
        "活动建议已存在",
        "同一仓库/SKU 已有待审批建议，请先在审批箱处理该建议，无需重复发起。",
    ),
    "PLAN_STALE": ("决策输入已变化", "审批或建单前检测到输入变化，请查看变化字段；可排除变化明细，或重新生成修订版。"),
    "RESOURCE_BUSY": ("资源忙", "事务锁超时，请稍后以同一幂等键重试。"),
    # 幂等
    "IDEMPOTENCY_KEY_REUSED": ("幂等键重复且载荷不同", "同一幂等键被用于不同请求载荷，已拒绝；请勿复用旧键。"),
    # 外部
    "EXTERNAL_UNKNOWN": ("外部订单状态未知", "只能通过查询供应商状态恢复，不允许盲目重试或换键重下。"),
    # 观测 / 模型
    "LLM_UNAVAILABLE": ("模型服务不可用", "已回退离线演示模式；请检查 LLM 配置后重试。"),
}


def next_step(code: str | None) -> tuple[str, str] | None:
    """返回 (标题, 下一步动作)；未知码返回 None（由前端兜底文案）。"""
    if not code:
        return None
    return NEXT_STEP.get(code)


def blocked_line_summary(product_id: str, blocked_code: str | None, blocked_reason: str | None) -> dict:
    """构造前端可直接展示的阻断行摘要（含下一步动作）。"""
    hint = next_step(blocked_code)
    return {
        "product_id": product_id,
        "blocked_code": blocked_code,
        "blocked_reason": blocked_reason,
        "next_step": hint[1] if hint else "请查看执行记录或联系管理员。",
    }
