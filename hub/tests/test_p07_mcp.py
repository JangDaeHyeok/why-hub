"""P07 검증 — MCP 서버 · 읽기 4종 (FastMCP in-memory Client).

- 검색·조회·목록·이력 도구 동작, 결과에 출처(id + anchor) 포함
- 도구 시그니처가 기획안1 §11 과 일치
- 도구는 service 호출만(로직 중복 없음)
"""

from __future__ import annotations

import asyncio

import pytest

from fastmcp import Client

from hub.interfaces.mcp_server import build_mcp
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
def mcp(tmp_path):
    svc = KnowledgeService(tmp_path)
    svc.save_document(_adr(), actor="alice", now="2026-07-14T10:00:00")
    svc.save_document(
        _adr(decision="서버 세션 + Redis 로 완전히 전환한다."),
        actor="bob", now="2026-07-14T11:00:00",
    )
    svc.save_document(
        _adr(id="guide-0001").replace("type: adr", "type: guide")
        .replace("# 대안\n\nJWT 방식은 폐기 지연으로 기각.\n\n", ""),
        actor="a", now="2026-07-14T12:00:00",
    )
    m = build_mcp(svc)
    yield m
    svc.close()


def _call(mcp, name, args):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args)

    return asyncio.run(go()).data


def test_tools_registered_match_spec(mcp):
    async def go():
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}

    names = asyncio.run(go())
    # 읽기 4종(+ get_docs_diff). save_document(쓰기)은 P09 에서 추가됨.
    assert {
        "search_knowledge", "get_document", "list_documents",
        "get_history", "get_docs_diff",
    } <= names


def test_search_knowledge_returns_source_anchor(mcp):
    hits = _call(mcp, "search_knowledge", {"query": "세션"})
    assert hits
    assert hits[0]["doc_id"] == "adr-0001"
    assert hits[0]["anchor"] == "결정"  # 출처 앵커
    assert hits[0]["title"] == "인증 방식"


def test_search_with_filters(mcp):
    hits = _call(mcp, "search_knowledge", {"query": "세션", "filters": {"type": "adr"}})
    assert all(h["type"] == "adr" for h in hits)


def test_get_document(mcp):
    doc = _call(mcp, "get_document", {"id": "adr-0001"})
    assert doc["id"] == "adr-0001"
    assert "서버 세션" in doc["body"]
    assert "결정" in [a["slug"] for a in doc["anchors"]]


def test_get_document_missing(mcp):
    assert _call(mcp, "get_document", {"id": "nope-0001"}) is None


def test_list_documents_filter(mcp):
    adrs = _call(mcp, "list_documents", {"type": "adr"})
    assert [d["id"] for d in adrs] == ["adr-0001"]
    all_docs = _call(mcp, "list_documents", {})
    assert {d["id"] for d in all_docs} == {"adr-0001", "guide-0001"}


def test_get_history(mcp):
    entries = _call(mcp, "get_history", {"id": "adr-0001"})
    assert [e["type"] for e in entries] == ["created", "revision"]
    dec = _call(mcp, "get_history", {"id": "adr-0001", "anchor": "결정"})
    assert all(e["anchor"] == "결정" for e in dec)


def test_get_docs_diff_empty_when_none(mcp):
    assert _call(mcp, "get_docs_diff", {"id": "adr-0001"}) == []
