"""M7 검증 — 쓰기 UI (직접 작성 + save 경유 + lint 피드백).

- 새 문서/편집 폼 렌더(템플릿 프리필)
- 저장 → save 루틴 경유 → 리다이렉트 → 문서 반영
- lint 실패 → 저장 차단 + 사유 배너 + 입력 보존(422)
- 편집 폼이 원문 로드
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from hub.interfaces.web import build_web_app
from hub.service import KnowledgeService


def _adr(id="adr-0001", alt="JWT 방식은 폐기 지연으로 기각.", decision="서버 세션 방식과 Redis 를 쓴다."):
    return (
        f"---\nid: {id}\ntype: adr\ntitle: 인증 방식\nstatus: accepted\n"
        "created: 2026-06-01\ntags: [auth]\n---\n\n"
        "# 배경\n\n인증 만료 처리가 어렵다.\n\n"
        f"# 결정\n\n{decision}\n\n"
        "# 근거\n\n즉시 폐기가 가능하다.\n\n"
        f"# 대안\n\n{alt}\n\n"
        "# 결과\n\n로그인이 서버 세션에 의존한다.\n"
    )


@pytest.fixture()
def env(tmp_path):
    svc = KnowledgeService(tmp_path)
    client = TestClient(build_web_app(svc))
    yield svc, client
    svc.close()


def test_new_form_renders(env):
    _, c = env
    r = c.get("/ui/new")
    assert r.status_code == 200
    assert 'name="markdown"' in r.text
    # actor 입력란은 제거됨 — 작성자는 인증 세션에서 결정된다(사용자 입력 신뢰 제거).
    assert 'name="actor"' not in r.text


def test_new_form_prefills_template(env):
    _, c = env
    r = c.get("/ui/new", params={"template": "adr"})
    assert r.status_code == 200
    assert "# 배경" in r.text and "# 대안" in r.text  # ADR 템플릿 골격


def test_save_valid_redirects_and_persists(env):
    svc, c = env
    r = c.post(
        "/ui/save",
        data={"markdown": _adr(), "actor": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/docs/adr-0001"
    # 실제 저장됨(save 경유)
    assert svc.get_document("adr-0001") is not None
    assert [e["type"] for e in svc.get_history("adr-0001")] == ["created"]


def test_save_lint_failure_blocks_with_reasons(env):
    svc, c = env
    r = c.post("/ui/save", data={"markdown": _adr(alt=""), "actor": "a"})  # 대안 비어 있음
    assert r.status_code == 422
    assert "lint 실패" in r.text
    assert "대안" in r.text
    # 입력 보존(마크다운이 폼에 남아 있음)
    assert "# 배경" in r.text
    # 저장 안 됨
    assert svc.get_document("adr-0001") is None


def test_edit_form_loads_raw(env):
    svc, c = env
    svc.save_document(_adr(), actor="a", now="2026-07-14T10:00:00")
    r = c.get("/ui/docs/adr-0001/edit")
    assert r.status_code == 200
    assert "id: adr-0001" in r.text  # 원문 frontmatter 로드
    assert "서버 세션 방식과 Redis" in r.text


def test_edit_then_save_creates_revision(env):
    svc, c = env
    svc.save_document(_adr(), actor="a", now="2026-07-14T10:00:00")
    edited = _adr(decision="서버 세션 + Redis 로 완전히 전환한다.")
    r = c.post("/ui/save", data={"markdown": edited, "actor": "bob"}, follow_redirects=False)
    assert r.status_code == 303
    assert [e["type"] for e in svc.get_history("adr-0001")] == ["created", "revision"]


def test_edit_missing_404(env):
    _, c = env
    assert c.get("/ui/docs/nope-0001/edit").status_code == 404


def test_new_doc_link_in_topbar(env):
    _, c = env
    assert "/ui/new" in c.get("/").text
