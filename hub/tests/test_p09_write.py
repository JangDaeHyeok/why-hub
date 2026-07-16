"""P09 검증 — 쓰기 경로 + docs-diff (MCP save_document + HTTP PUT).

- 쓰기 → 이력 생성 → 검색 반영
- 잘못된 문서(대안 섹션 없음) → 인터페이스별 lint 사유 전달(HTTP 422 / MCP 에러)
- docs-diff 기록·조회
- 생성→save→검색→조회 전 경로 통과(워킹 스켈레톤 수용)
"""

from __future__ import annotations

import asyncio

import pytest

from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from hub.interfaces.http_api import build_app
from hub.interfaces.mcp_server import build_mcp
from hub.service import KnowledgeService


def _adr(id="adr-0001", status="accepted", decision="서버 세션 방식과 Redis 를 쓴다.", alt="JWT 방식은 폐기 지연으로 기각."):
    return (
        f"---\nid: {id}\ntype: adr\ntitle: 인증 방식\nstatus: {status}\n"
        "created: 2026-06-01\ntags: [auth]\n---\n\n"
        "# 배경\n\n인증 만료 처리가 어렵다.\n\n"
        f"# 결정\n\n{decision}\n\n"
        "# 근거\n\n즉시 폐기가 가능하다.\n\n"
        f"# 대안\n\n{alt}\n\n"
        "# 결과\n\n로그인이 서버 세션에 의존한다.\n"
    )


# ── MCP 쓰기 ──────────────────────────────────────────────────────────
def _mcp_call(mcp, name, args):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args)

    return asyncio.run(go()).data


@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path)
    yield s
    s.close()


def test_mcp_save_creates_and_searchable(svc):
    mcp = build_mcp(svc)
    res = _mcp_call(mcp, "save_document", {"markdown": _adr()})
    assert res["id"] == "adr-0001" and res["change_type"] == "created"

    hits = _mcp_call(mcp, "search_knowledge", {"query": "세션"})
    assert any(h["doc_id"] == "adr-0001" for h in hits)
    hist = _mcp_call(mcp, "get_history", {"id": "adr-0001"})
    assert [e["type"] for e in hist] == ["created"]


def test_mcp_save_lint_failure_raises(svc):
    mcp = build_mcp(svc)
    with pytest.raises(ToolError) as ei:
        _mcp_call(mcp, "save_document", {"markdown": _adr(alt="")})
    assert "대안" in str(ei.value)


def test_mcp_save_with_intended_diff(svc):
    mcp = build_mcp(svc)
    _mcp_call(mcp, "save_document", {
        "markdown": _adr(),
        "intended_diff": "의도: 세션 기반 전환.",
    })
    diffs = _mcp_call(mcp, "get_docs_diff", {"id": "adr-0001"})
    assert diffs and "의도" in diffs[0]["content"]


# ── HTTP 쓰기 ─────────────────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path):
    s = KnowledgeService(tmp_path)
    with TestClient(build_app(s)) as c:
        yield c
    s.close()


def test_http_put_create_search_read_roundtrip(client):
    # 생성 → save
    r = client.put("/docs/adr-0001", json={"markdown": _adr(), "actor": "alice"})
    assert r.status_code == 200
    assert r.json()["change_type"] == "created"

    # 조회
    assert client.get("/docs/adr-0001").json()["id"] == "adr-0001"
    # 검색 반영
    hits = client.get("/search", params={"q": "세션"}).json()
    assert any(h["doc_id"] == "adr-0001" for h in hits)
    # 이력 생성
    assert [e["type"] for e in client.get("/docs/adr-0001/history").json()] == ["created"]


def test_http_put_revision_records_history(client):
    client.put("/docs/adr-0001", json={"markdown": _adr(), "actor": "a"})
    client.put("/docs/adr-0001", json={
        "markdown": _adr(decision="서버 세션 + Redis 로 완전히 전환한다."), "actor": "b"})
    hist = client.get("/docs/adr-0001/history").json()
    assert [e["type"] for e in hist] == ["created", "revision"]


def test_http_put_lint_failure_422_with_reasons(client):
    r = client.put("/docs/adr-0001", json={"markdown": _adr(alt=""), "actor": "a"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "lint"
    assert any("대안" in reason for reason in body["reasons"])
    # 저장되지 않았어야 함
    assert client.get("/docs/adr-0001").status_code == 404


def test_http_put_id_mismatch_422(client):
    r = client.put("/docs/adr-9999", json={"markdown": _adr(id="adr-0001"), "actor": "a"})
    assert r.status_code == 422


def test_http_put_with_intended_diff_recorded(client):
    client.put("/docs/adr-0001", json={
        "markdown": _adr(), "actor": "a", "intended_diff": "의도: 세션 전환."})
    diffs = client.get("/docs/adr-0001/diff").json()
    assert diffs and "의도" in diffs[0]["content"]
