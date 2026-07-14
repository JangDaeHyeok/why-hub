"""M6 검증 — 읽기 UI (FastAPI + HTMX, TestClient).

- 목록·검색·조회·이력·계보 페이지 렌더
- 검색 HTMX 부분 스왑(조각) + 전체 페이지 폴백
- 정적 디자인 토큰(라이트/다크) 제공, FOUC 방지 테마 init
- UI 는 service 경유(파일 직접 접근 없음)
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from hub.interfaces.web import build_web_app
from hub.service import KnowledgeService


def _adr(id="adr-0001", decision="서버 세션 방식과 Redis 를 쓴다.", supersedes=None):
    extra = f"supersedes: {supersedes}\n" if supersedes else ""
    return (
        f"---\nid: {id}\ntype: adr\ntitle: 인증 방식 {id}\nstatus: accepted\n"
        f"created: 2026-06-01\ntags: [auth]\n{extra}---\n\n"
        "# 배경\n\n인증 만료 처리가 어렵다.\n\n"
        f"# 결정\n\n{decision}\n\n"
        "# 근거\n\n즉시 폐기가 가능하다.\n\n"
        "# 대안\n\nJWT 방식은 폐기 지연으로 기각.\n\n"
        "# 결과\n\n로그인이 서버 세션에 의존한다.\n"
    )


@pytest.fixture()
def client(tmp_path):
    svc = KnowledgeService(tmp_path)
    svc.save_document(_adr("adr-0002"), actor="a", now="2026-07-14T10:00:00")
    svc.save_document(_adr("adr-0001", supersedes="adr-0002"), actor="a", now="2026-07-14T10:01:00")
    svc.save_document(
        _adr("adr-0001", decision="서버 세션 + Redis 로 완전히 전환한다.", supersedes="adr-0002"),
        actor="b", now="2026-07-14T11:00:00",
    )
    with TestClient(build_web_app(svc)) as c:
        yield c
    svc.close()


def test_home_lists_documents(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "인증 방식 adr-0001" in r.text
    # 테마 토큰·FOUC 방지 스크립트 존재
    assert 'data-theme' in r.text
    assert "prefers-color-scheme" in r.text


def test_static_tokens_light_and_dark(client):
    r = client.get("/static/css/tokens.css")
    assert r.status_code == 200
    assert "--bg-canvas" in r.text
    assert '[data-theme="dark"]' in r.text  # 다크 테마 매핑 존재


def test_search_htmx_fragment(client):
    r = client.get("/ui/search", params={"q": "세션"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    # 조각(전체 페이지 아님): <html> 없이 결과만
    assert "<html" not in r.text.lower()
    assert "adr-0001" in r.text
    assert "#결정" in r.text  # 출처 앵커


def test_search_full_page_fallback(client):
    r = client.get("/ui/search", params={"q": "세션"})  # HX-Request 없음
    assert r.status_code == 200
    assert "<html" in r.text.lower()  # 전체 페이지
    assert "adr-0001" in r.text


def test_document_view_renders_markdown(client):
    r = client.get("/ui/docs/adr-0001")
    assert r.status_code == 200
    assert "<h1>배경</h1>" in r.text  # 마크다운 헤더 렌더
    assert "서버 세션 + Redis" in r.text
    # 탭 존재
    assert "/ui/docs/adr-0001/history" in r.text


def test_document_404(client):
    assert client.get("/ui/docs/nope-0001").status_code == 404


def test_history_view_shows_delta(client):
    r = client.get("/ui/docs/adr-0001/history")
    assert r.status_code == 200
    assert "created" in r.text and "revision" in r.text
    # delta +/- 줄이 상태색 span 으로
    assert 'class="add"' in r.text or 'class="del"' in r.text


def test_related_view_shows_lineage(client):
    r = client.get("/ui/docs/adr-0001/related")
    assert r.status_code == 200
    assert "supersedes" in r.text
    assert "adr-0002" in r.text  # adr-0001 이 adr-0002 를 대체


def test_json_api_still_available(client):
    # build_web_app 은 JSON API 도 그대로 제공.
    r = client.get("/search", params={"q": "세션"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)
