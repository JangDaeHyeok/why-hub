"""P06 검증 — 서비스 레이어(인터페이스 독립).

- MCP·HTTP 없이도 전 기능(검색·조회·목록·이력·의도된 변경·저장) 호출 가능
- 서비스는 store 위의 얇은 파사드, actor 는 인자로 받음(인증은 인터페이스가 채움)
"""

from __future__ import annotations

import pytest

from hub.service import KnowledgeService


def _adr(id="adr-0001", status="accepted", decision="서버 세션 방식과 Redis 를 쓴다."):
    return (
        f"---\nid: {id}\ntype: adr\ntitle: 인증 방식\nstatus: {status}\n"
        "created: 2026-06-01\ntags: [auth]\n---\n\n"
        "# 배경\n\n인증 만료 처리가 어렵다.\n\n"
        f"# 결정\n\n{decision}\n\n"
        "# 근거\n\n즉시 폐기가 가능하다.\n\n"
        "# 대안\n\nJWT 방식은 폐기 지연으로 기각.\n\n"
        "# 결과\n\n로그인이 서버 세션에 의존한다.\n"
    )


@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path)
    yield s
    s.close()


def test_save_and_get_document(svc):
    res = svc.save_document(_adr(), actor="alice", now="2026-07-14T10:00:00")
    assert res.id == "adr-0001" and res.change_type == "created"

    doc = svc.get_document("adr-0001")
    assert doc is not None
    assert doc["id"] == "adr-0001" and doc["type"] == "adr"
    assert "서버 세션" in doc["body"]
    # 앵커 목록으로 섹션 네비게이션 가능
    assert "결정" in [a["slug"] for a in doc["anchors"]]


def test_get_document_missing_returns_none(svc):
    assert svc.get_document("nope-0001") is None


def test_search_knowledge_returns_source_anchor(svc):
    svc.save_document(_adr(), actor="a", now="2026-07-14T10:00:00")
    hits = svc.search_knowledge("세션")
    assert hits
    assert hits[0]["doc_id"] == "adr-0001"
    assert hits[0]["anchor"] == "결정"
    # 상위 결과에 frontmatter 요약(title/type) 첨부
    assert hits[0]["title"] == "인증 방식"
    assert hits[0]["type"] == "adr"


def test_search_filter_precedes(svc):
    svc.save_document(_adr(id="adr-0001"), actor="a", now="2026-07-14T10:00:00")
    svc.save_document(
        _adr(id="adr-0002", status="deprecated"), actor="a", now="2026-07-14T10:01:00"
    )
    hits = svc.search_knowledge("세션", filters={"status": "accepted"})
    assert all(h["doc_id"] == "adr-0001" for h in hits)


def test_list_documents(svc):
    svc.save_document(_adr(id="adr-0001"), actor="a", now="2026-07-14T10:00:00")
    svc.save_document(
        "---\nid: guide-0001\ntype: guide\ntitle: G\nstatus: proposed\ncreated: 2026-01-01\n---\n\n# H\n\nx\n",
        actor="a", now="2026-07-14T10:01:00",
    )
    all_docs = svc.list_documents()
    assert {d["id"] for d in all_docs} == {"adr-0001", "guide-0001"}
    adrs = svc.list_documents(filters={"type": "adr"})
    assert [d["id"] for d in adrs] == ["adr-0001"]


def test_get_history(svc):
    svc.save_document(_adr(), actor="alice", now="2026-07-14T10:00:00")
    svc.save_document(
        _adr(decision="서버 세션 + Redis 로 완전히 전환한다."),
        actor="bob", now="2026-07-14T11:00:00",
    )
    entries = svc.get_history("adr-0001")
    assert [e["type"] for e in entries] == ["created", "revision"]
    # 앵커 필터
    dec = svc.get_history("adr-0001", anchor="결정")
    assert all(e["anchor"] == "결정" for e in dec)
    assert any(e["type"] == "revision" for e in dec)


def test_get_docs_diff(svc):
    svc.save_document(
        _adr(), actor="a", now="2026-07-14T10:00:00",
        intended_diff="의도: 결정 섹션을 세션 기반으로 변경한다.",
    )
    diffs = svc.get_docs_diff("adr-0001")
    assert len(diffs) == 1
    assert diffs[0]["date"] == "2026-07-14"
    assert "의도" in diffs[0]["content"]


# get_related(P11)·ingest_source/curate(P12) 는 각 Phase 테스트에서 검증한다.
