"""向量与关键词召回融合：RRF（需求 8.1 / 架构 5）。"""

from __future__ import annotations

RRF_K = 60


def rrf_merge(
    *ranked_lists: list[tuple[str, float]],
    top_k: int = 10,
) -> list[tuple[str, float]]:
    """RRF 融合多路按分数降序的 (id, score) 列表，返回融合分数降序结果。

    分数仅用于内部排序，不对外承诺物理意义。
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (item_id, _score) in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (RRF_K + rank)
    merged = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return merged[:top_k]
