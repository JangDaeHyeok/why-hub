"""P08 검증 — HTTP API · 읽기 (FastAPI TestClient).

- 각 엔드포인트 응답·에러 코드(404), 결과에 출처 앵커 포함
- 엔드포인트가 기획안1 §11 HTTP 열과 일치, service 호출만
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from hub.interfaces.http_api import build_app
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
def client(tmp_path):
    svc = KnowledgeService(tmp_path)
    svc.save_document(_adr(), actor="alice", now="2026-07-14T10:00:00")
    svc.save_document(
        _adr(decision="서버 세션 + Redis 로 완전히 전환한다."),
        actor="bob", now="2026-07-14T11:00:00",
        intended_diff="의도: 결정 섹션 세션 기반 전환.",
    )
    with TestClient(build_app(svc)) as c:
        yield c
    svc.close()


def test_search_returns_source_anchor(client):
    r = client.get("/search", params={"q": "세션"})
    assert r.status_code == 200
    hits = r.json()
    assert hits[0]["doc_id"] == "adr-0001"
    assert hits[0]["anchor"] == "결정"  # 출처 앵커


def test_search_with_filters(client):
    r = client.get("/search", params={"q": "세션", "type": "adr"})
    assert r.status_code == 200
    assert all(h["type"] == "adr" for h in r.json())


def test_list_docs(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert [d["id"] for d in r.json()] == ["adr-0001"]


def test_get_doc(client):
    r = client.get("/docs/adr-0001")
    assert r.status_code == 200
    doc = r.json()
    assert doc["id"] == "adr-0001"
    assert "서버 세션" in doc["body"]


def test_get_doc_404(client):
    r = client.get("/docs/nope-0001")
    assert r.status_code == 404


def test_history(client):
    r = client.get("/docs/adr-0001/history")
    assert r.status_code == 200
    assert [e["type"] for e in r.json()] == ["created", "revision"]


def test_history_404_for_missing_doc(client):
    assert client.get("/docs/nope-0001/history").status_code == 404


def test_diff(client):
    r = client.get("/docs/adr-0001/diff")
    assert r.status_code == 200
    diffs = r.json()
    assert diffs and diffs[0]["date"] == "2026-07-14"
    assert "의도" in diffs[0]["content"]


def test_diff_404_for_missing_doc(client):
    assert client.get("/docs/nope-0001/diff").status_code == 404
