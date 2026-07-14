"""M5 검증 — 평가 셋 / 골든 질의 (docs/specs/구현스펙-평가셋-M5.md §4).

- 지표 함수 단위 테스트(손계산과 일치)
- 골든 집계: recall@5 ≥ 0.9, MRR ≥ 0.8
- 필터 선행(네거티브)
- 한국어 토크나이저 한계(조사 결합 질의 under-match) 고정
"""

from __future__ import annotations

import pytest

from hub.eval import golden, metrics
from hub.eval.harness import evaluate
from hub.service import KnowledgeService


# ── 지표 단위 ─────────────────────────────────────────────────────────
def test_metrics_hand_computed():
    ranked = ["b", "a", "c"]  # a 가 2번째
    rel = {"a"}
    assert metrics.recall_at_k(ranked, rel, 5) == 1.0
    assert metrics.recall_at_k(ranked, rel, 1) == 0.0  # top-1 은 b
    assert metrics.reciprocal_rank(ranked, rel) == pytest.approx(0.5)
    assert metrics.precision_at_k(["a", "x"], {"a"}, 2) == pytest.approx(0.5)


def test_metrics_no_relevant_hit():
    assert metrics.reciprocal_rank(["x", "y"], {"a"}) == 0.0
    assert metrics.recall_at_k(["x"], {"a"}, 5) == 0.0


def test_dedup_preserves_best_rank():
    assert metrics.dedup(["a", "a", "b", "a"]) == ["a", "b"]


# ── 골든 평가 ─────────────────────────────────────────────────────────
@pytest.fixture()
def seeded(tmp_path):
    svc = KnowledgeService(tmp_path)
    golden.seed_corpus(svc)
    yield svc
    svc.close()


def test_golden_meets_quality_thresholds(seeded):
    result = evaluate(seeded, golden.GOLDEN, k=5)
    agg = result["aggregate"]
    assert agg["recall_at_k"] >= 0.9, result["per_query"]
    assert agg["mrr"] >= 0.8, result["per_query"]
    # 각 질의가 자기 문서를 실제로 찾는다.
    for q in result["per_query"]:
        assert q["recall"] == 1.0, q


def test_filter_precedes_ranking_negative(seeded):
    # 필터로 배제되면 후보에서 빠진다: 'delta' 는 adr-0002 에 있지만 type=guide 로 거르면 0건.
    result = evaluate(seeded, [{"query": "delta", "relevant": ["adr-0002"],
                                "filters": {"type": "guide"}}], k=5)
    q = result["per_query"][0]
    assert q["retrieved"] == []
    assert q["recall"] == 0.0


def test_korean_tokenizer_limitation_documented(seeded):
    # unicode61 은 조사를 분리 못 해 "앵커를" 이 "앵커" 토큰과 매치되지 않는다([Δ] §7 후속 과제).
    result = evaluate(seeded, golden.HARD_QUERIES, k=5)
    hard = result["per_query"][0]
    assert hard["recall"] == 0.0, "조사 결합 질의가 예상과 달리 매치됨 — 한계 가정 재검토 필요"
    # 반면 독립 토큰 '앵커' 는 정상 매치(대조군)
    ok = evaluate(seeded, [{"query": "앵커", "relevant": ["adr-0003"]}], k=5)
    assert ok["per_query"][0]["recall"] == 1.0
