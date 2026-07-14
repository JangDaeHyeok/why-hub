"""검색 품질 지표 — 순수 함수 (M5 §3).

순위(ranked)는 search 결과 doc_id 를 **중복 제거한 순서**(best rank first).
relevant 는 그 질의에 맞다고 합의된 doc_id 집합.
"""

from __future__ import annotations

from collections.abc import Iterable


def dedup(ranked: Iterable[str]) -> list[str]:
    """순서 보존 중복 제거(앵커 여러 개가 같은 문서를 가리킬 때 문서 단위로)."""
    seen: set[str] = set()
    out: list[str] = []
    for x in ranked:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    topk = set(ranked[:k])
    return len(topk & relevant) / len(relevant)


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    topk = ranked[:k]
    if not topk:
        return 0.0
    return len([x for x in topk if x in relevant]) / len(topk)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for i, doc in enumerate(ranked, start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
