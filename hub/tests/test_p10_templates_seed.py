"""P10 검증 — 템플릿 + 세션 포크 캡처 워크플로우 + 도그푸딩 시드.

- 템플릿 헤더가 lint 필수 섹션(배경/결정/근거/대안/결과)과 정렬
- 템플릿으로 만든(=시드) 문서가 lint 통과 + 저장·검색됨
- 워크플로우 문서가 자기 완결적(핵심 절차 포함)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hub.config import Config
from hub.service import KnowledgeService
from hub.store import anchors as anchors_mod
from hub.store.lint import lint
from hub.store.normalize import normalize
from scripts.seed_knowledge import SEED_ADRS, seed

REPO = Path(__file__).resolve().parents[2]
REQUIRED = {"배경", "결정", "근거", "대안", "결과"}


# ── 템플릿 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["adr.md", "design-intent.md"])
def test_template_headers_align_with_required_sections(name):
    nd = normalize((REPO / "templates" / name).read_text(encoding="utf-8"))
    slugs = {a.slug for a in anchors_mod.parse_anchors(nd.body)}
    assert REQUIRED <= slugs, f"{name} 누락 섹션: {REQUIRED - slugs}"


def test_adr_template_frontmatter_shape():
    nd = normalize((REPO / "templates" / "adr.md").read_text(encoding="utf-8"))
    assert nd.frontmatter.get("type") == "adr"
    # 플레이스홀더 id 라 그대로는 lint 를 통과하지 않는다(채워야 저장 가능).
    from hub.store.lint import LintError

    with pytest.raises(LintError):
        lint(nd, Config())


# ── 도그푸딩 시드 ─────────────────────────────────────────────────────
def test_seed_docs_pass_lint():
    # 정본 ADR 원문 각각이 lint 를 통과한다(필수 섹션·대안 채워짐).
    for md in SEED_ADRS:
        lint(normalize(md), Config())  # 예외 없어야 함


def test_seed_saves_and_searchable(tmp_path):
    results = seed(tmp_path)
    assert [r.id for r in results] == ["adr-0001", "adr-0002", "adr-0003"]
    assert all(r.change_type == "created" for r in results)

    svc = KnowledgeService(tmp_path)
    try:
        # 저장된 결정이 검색됨 (출처 앵커 포함)
        hits = svc.search_knowledge("FTS5")
        assert any(h["doc_id"] == "adr-0001" for h in hits)
        # 계보 참조(related)가 dangling 없이 저장됨
        docs = {d["id"] for d in svc.list_documents()}
        assert {"adr-0001", "adr-0002", "adr-0003"} <= docs
        # 이력이 created 로 쌓임
        assert [e["type"] for e in svc.get_history("adr-0002")] == ["created"]
    finally:
        svc.close()


# ── 워크플로우 문서 ───────────────────────────────────────────────────
def test_workflow_doc_is_self_contained():
    text = (REPO / "docs" / "workflow-adr-capture.md").read_text(encoding="utf-8")
    for token in ["/branch", "save_document", "포크", "대안", "폐기"]:
        assert token in text, f"워크플로우 문서에 '{token}' 누락"
