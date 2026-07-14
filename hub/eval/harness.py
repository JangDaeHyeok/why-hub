"""평가 하네스 — 골든 질의를 돌려 지표를 집계 (M5 §3).

`evaluate(service, golden, k)` → per-query 결과 + aggregate. service 는 읽기만 한다.
"""

from __future__ import annotations

from . import metrics


def evaluate(service, golden: list[dict], k: int = 5) -> dict:
    per_query: list[dict] = []
    for g in golden:
        hits = service.search_knowledge(g["query"], g.get("filters"), k=max(k, 10))
        ranked = metrics.dedup([h["doc_id"] for h in hits])
        relevant = set(g["relevant"])
        per_query.append(
            {
                "query": g["query"],
                "relevant": sorted(relevant),
                "retrieved": ranked[:k],
                "recall": metrics.recall_at_k(ranked, relevant, k),
                "precision": metrics.precision_at_k(ranked, relevant, k),
                "rr": metrics.reciprocal_rank(ranked, relevant),
            }
        )

    aggregate = {
        "k": k,
        "n": len(per_query),
        "recall_at_k": metrics.mean([q["recall"] for q in per_query]),
        "precision_at_k": metrics.mean([q["precision"] for q in per_query]),
        "mrr": metrics.mean([q["rr"] for q in per_query]),
    }
    return {"per_query": per_query, "aggregate": aggregate}


def format_report(result: dict) -> str:
    """사람이 읽는 표 형태 리포트."""
    lines = []
    lines.append(f"{'query':<12} {'recall':>7} {'prec':>6} {'rr':>6}  retrieved")
    lines.append("-" * 60)
    for q in result["per_query"]:
        lines.append(
            f"{q['query']:<12} {q['recall']:>7.2f} {q['precision']:>6.2f} "
            f"{q['rr']:>6.2f}  {q['retrieved']}"
        )
    a = result["aggregate"]
    lines.append("-" * 60)
    lines.append(
        f"AGG (k={a['k']}, n={a['n']}): recall@k={a['recall_at_k']:.3f}  "
        f"precision@k={a['precision_at_k']:.3f}  MRR={a['mrr']:.3f}"
    )
    return "\n".join(lines)
