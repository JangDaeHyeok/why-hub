"""프로젝트별 접근 권한(ACL) 검증 (구현스펙-인증인가-RBAC.md §ACL).

- 서비스: viewer/editor·기본 프로젝트 공개·admin 전권, 검색 필터-선행, frontmatter 우회 차단
- HTTP 관리 UI: admin 프로젝트 생성·멤버 관리, 비admin 차단
- MCP: JWT projects 클레임으로 스코프·비허용 프로젝트 도구 거부(라이브 streamable-http)
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier

from hub.auth.principal import Principal
from hub.config import ApprovalConfig, Config
from hub.interfaces.http_api import build_app
from hub.interfaces.mcp_server import build_mcp
from hub.interfaces.web import build_web_app
from hub.service import KnowledgeService
from hub.tests.authhelpers import (
    admin,
    auth_client,
    keypair,
    login,
    make_auth_service,
    make_issuer,
    make_user,
)


def _adr(id, project=None):
    p = f"project: {project}\n" if project else ""
    return (
        f"---\nid: {id}\ntype: adr\ntitle: {id}\nstatus: accepted\ncreated: 2026-06-01\n{p}---\n\n"
        "# 배경\n세션 x\n# 결정\ny\n# 근거\nz\n# 대안\na\n# 결과\nb\n"
    )


def member(name, projects):
    return Principal.for_user(name, is_admin=False, projects=projects)


# ── 서비스 레벨 ─────────────────────────────────────────────────────────
@pytest.fixture()
def svc(tmp_path):
    cfg = Config(); cfg.default_project = "why-hub"
    s = KnowledgeService(tmp_path, cfg)
    s.save_document(_adr("adr-0001"), actor="sys")                     # default
    s.save_document(_adr("adr-0002", "alpha"), actor="sys", project="alpha")
    s.save_document(_adr("adr-0003", "beta"), actor="sys", project="beta")
    yield s
    s.close()


def test_member_reads_default_and_granted_only(svc):
    carol = member("carol", {"alpha": "viewer"})
    ids = {d["id"] for d in svc.list_documents(principal=carol)}
    assert ids == {"adr-0001", "adr-0002"}  # default + alpha, NOT beta
    assert svc.get_document("adr-0002", principal=carol)["id"] == "adr-0002"
    assert svc.get_document("adr-0003", principal=carol) is None  # beta 차단
    assert "beta" not in svc.list_projects(principal=carol)


def test_search_filter_precedes_ranking(svc):
    carol = member("carol", {"alpha": "editor"})
    hits = {h["doc_id"] for h in svc.search_knowledge("세션", principal=carol)}
    assert hits == {"adr-0001", "adr-0002"}  # beta 문서는 검색에서 제외


def test_viewer_cannot_write_editor_can(svc):
    viewer = member("v", {"alpha": "viewer"})
    editor = member("e", {"alpha": "editor"})
    with pytest.raises(PermissionError):
        svc.save_document(_adr("adr-0009", "alpha"), actor="v", project="alpha", principal=viewer)
    res = svc.save_document(_adr("adr-0009", "alpha"), actor="e", project="alpha", principal=editor)
    assert res.id == "adr-0009"


def test_default_project_writable_by_any_member(svc):
    carol = member("carol", {})  # 멤버십 없음
    res = svc.save_document(_adr("adr-0010"), actor="carol", project="why-hub", principal=carol)
    assert res.id == "adr-0010"


def test_frontmatter_bypass_blocked(svc):
    # 기본 프로젝트로 스코프했더라도 frontmatter 에 beta 를 심으면 beta 권한이 필요(우회 차단).
    carol = member("carol", {})
    with pytest.raises(PermissionError):
        svc.save_document(_adr("adr-0011", "beta"), actor="carol", project="why-hub", principal=carol)


def test_chat_deprecate_respects_acl(svc):
    # 챗 propose_deprecate 가 접근 불가 문서 원문을 staged 로 유출하지 않아야 한다.
    orch = svc._orchestrator()
    sess = {"staged": [], "project": "why-hub", "principal": member("carol", {})}
    res = orch._stage_deprecate(sess, "adr-0003", "중복", None)  # beta — 접근 불가
    assert "error" in res and sess["staged"] == []              # 유출·staged 없음
    ok = orch._stage_deprecate(sess, "adr-0001", "중복", None)  # default — 접근 가능
    assert ok.get("staged") is True


def test_cross_project_move_blocked(svc):
    # alpha editor 가 전역 id 가 같은 beta 문서를 project=alpha 로 덮어써 이동시키는 우회 차단
    # (원본 beta 쓰기권한이 없으므로 거부 — 코드리뷰 2).
    alpha_editor = member("carol", {"alpha": "editor"})
    with pytest.raises(PermissionError):
        svc.save_document(_adr("adr-0003", "alpha"), actor="carol", project="alpha",
                          principal=alpha_editor)
    # 원본 문서는 그대로 beta 소속(이동되지 않음).
    assert svc.get_document("adr-0003", principal=admin("al"))["project"] == "beta"


def test_cross_project_move_allowed_with_both(svc):
    # 원본·목적지 양쪽 editor 면 이동 허용(정책: 크로스프로젝트 이동 = 양쪽 권한 필요).
    both = member("carol", {"alpha": "editor", "beta": "editor"})
    res = svc.save_document(_adr("adr-0003", "alpha"), actor="carol", project="alpha",
                            principal=both)
    assert res.id == "adr-0003"
    assert svc.get_document("adr-0003", principal=admin("al"))["project"] == "alpha"


def test_cross_project_move_blocked_on_submit(tmp_path):
    # 승인 워크플로우: 이동 제출도 submit 시점에 원본 프로젝트 권한을 요구(우회 차단).
    cfg = Config(); cfg.default_project = "why-hub"; cfg.approval = ApprovalConfig(enabled=True)
    s = KnowledgeService(tmp_path, cfg)
    s.config.approval.enabled = False        # seed 는 즉시 반영
    s.save_document(_adr("adr-0001", "beta"), actor="sys", project="beta")
    s.config.approval.enabled = True
    alpha_editor = member("carol", {"alpha": "editor"})
    with pytest.raises(PermissionError):
        s.submit_change(_adr("adr-0001", "alpha"), actor="carol", op="edit",
                        doc_id="adr-0001", project="alpha", principal=alpha_editor)
    # 대기 큐에 남은 제출 없음.
    assert s.list_submissions("pending") == []
    s.close()


def test_admin_sees_and_writes_all(svc):
    a = admin("al")
    assert len({d["id"] for d in svc.list_documents(principal=a)}) == 3
    assert svc.get_document("adr-0003", principal=a)["id"] == "adr-0003"
    assert svc.save_document(_adr("adr-0012", "beta"), actor="al", project="beta", principal=a).id == "adr-0012"


def test_approve_requires_project_access(tmp_path):
    cfg = Config(); cfg.default_project = "why-hub"; cfg.approval = ApprovalConfig(enabled=True)
    s = KnowledgeService(tmp_path, cfg)
    editor = member("carol", {"alpha": "editor"})
    sub = s.save_document(_adr("adr-0001", "alpha"), actor="carol", project="alpha", principal=editor)
    assert sub["project"] == "alpha"
    # 리뷰어 권한이 없는 member 는 승인 불가; admin 은 전권으로 승인.
    with pytest.raises(PermissionError):
        s.approve_submission(sub["submission_id"], principal=member("bob", {"alpha": "editor"}))
    res = s.approve_submission(sub["submission_id"], principal=admin("al"))
    assert res.id == "adr-0001"
    s.close()


# ── HTTP 관리 UI (admin 전용) ───────────────────────────────────────────
@pytest.fixture()
def http_env(tmp_path):
    cfg = Config(); cfg.default_project = "why-hub"; cfg.approval = ApprovalConfig(enabled=False)
    s = KnowledgeService(tmp_path, cfg)
    auth = make_auth_service(tmp_path)
    make_user(auth, "alice", is_admin=True)
    carol = make_user(auth, "carol")
    app = build_app(s, auth)  # 관리 라우트는 web 앱에만 있으므로 web 필요 → 아래 web 테스트에서 검증

    def client_for(username):
        c = TestClient(app)
        tok, csrf = login(auth, username)
        return auth_client(c, auth, tok, csrf)

    yield s, auth, carol, client_for
    s.close(); auth.close()


def test_member_search_scoped_via_http(http_env):
    s, auth, carol, client_for = http_env
    # admin 이 alpha 생성 + carol 를 viewer 로. beta 문서 하나 생성.
    s.save_document(_adr("adr-0100", "beta"), actor="sys", project="beta")
    auth.create_project("alpha", "Alpha", None, created_by=None)
    auth.add_project_member("alpha", carol["id"], "viewer")
    s.save_document(_adr("adr-0101", "alpha"), actor="sys", project="alpha")
    carol_c = client_for("carol")
    hits = carol_c.get("/search", params={"q": "세션"}).json()
    ids = {h["doc_id"] for h in hits}
    assert "adr-0101" in ids and "adr-0100" not in ids  # alpha 보임, beta 안 보임
    # 직접 조회도 차단
    assert carol_c.get("/docs/adr-0100").status_code == 404


# ── 웹 관리 UI (admin 전용) ─────────────────────────────────────────────
@pytest.fixture()
def web_env(tmp_path):
    cfg = Config(); cfg.default_project = "why-hub"; cfg.approval = ApprovalConfig(enabled=False)
    s = KnowledgeService(tmp_path, cfg)
    auth = make_auth_service(tmp_path)
    make_user(auth, "alice", is_admin=True)
    make_user(auth, "carol")
    app = build_web_app(s, auth)

    def client_for(username):
        c = TestClient(app, follow_redirects=False)
        tok, csrf = login(auth, username)
        return auth_client(c, auth, tok, csrf), csrf

    yield s, auth, client_for
    s.close(); auth.close()


def test_non_admin_cannot_manage_projects(web_env):
    _, _auth, client_for = web_env
    carol, csrf = client_for("carol")
    assert carol.get("/ui/projects").status_code == 403
    assert carol.post("/ui/projects",
                      data={"slug": "x", "csrf_token": csrf}).status_code == 403


def test_admin_creates_project_and_grants_access(web_env):
    s, auth, client_for = web_env
    admin_c, acsrf = client_for("alice")
    # 생성
    r = admin_c.post("/ui/projects", data={"slug": "alpha", "name": "Alpha", "csrf_token": acsrf})
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/alpha"
    # alpha 문서 생성(admin)
    s.save_document(_adr("adr-0200", "alpha"), actor="alice", project="alpha",
                    principal=admin("alice"))
    carol_id = auth.repo.get_user_by_username("carol")["id"]
    # 부여 전: carol 은 alpha 문서 접근 불가
    carol_c, ccsrf = client_for("carol")
    assert carol_c.get("/ui/docs/adr-0200").status_code == 404
    # editor 부여
    r = admin_c.post("/ui/projects/alpha/members",
                     data={"user_id": carol_id, "role": "editor", "csrf_token": acsrf})
    assert r.status_code == 200
    # 부여 후: 새 세션에서 접근 가능
    carol_c2, _ = client_for("carol")
    assert carol_c2.get("/ui/docs/adr-0200").status_code == 200


def test_admin_deletes_project_revokes_access(web_env):
    s, auth, client_for = web_env
    admin_c, acsrf = client_for("alice")
    carol_id = auth.repo.get_user_by_username("carol")["id"]
    admin_c.post("/ui/projects", data={"slug": "alpha", "name": "Alpha", "csrf_token": acsrf})
    admin_c.post("/ui/projects/alpha/members",
                 data={"user_id": carol_id, "role": "editor", "csrf_token": acsrf})
    s.save_document(_adr("adr-0300", "alpha"), actor="alice", project="alpha",
                    principal=admin("alice"))
    # 삭제 전: carol 접근 가능
    carol_c, _ = client_for("carol")
    assert carol_c.get("/ui/docs/adr-0300").status_code == 200
    # 삭제
    r = admin_c.post("/ui/projects/alpha/delete", data={"csrf_token": acsrf})
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects"
    assert auth.get_project("alpha") is None
    assert auth.repo.projects_for_user(carol_id) == {}  # 멤버십 회수
    # 삭제 후: carol 접근 불가(문서는 남아 admin 만 접근)
    carol_c2, _ = client_for("carol")
    assert carol_c2.get("/ui/docs/adr-0300").status_code == 404
    assert admin_c.get("/ui/docs/adr-0300").status_code == 200  # 문서는 존속(admin)


def test_default_project_cannot_be_deleted(web_env):
    _, _auth, client_for = web_env
    admin_c, acsrf = client_for("alice")
    r = admin_c.post("/ui/projects/why-hub/delete", data={"csrf_token": acsrf})
    assert r.status_code == 400  # 기본 프로젝트 삭제 거부


def test_non_admin_cannot_delete_project(web_env):
    s, auth, client_for = web_env
    admin_c, acsrf = client_for("alice")
    admin_c.post("/ui/projects", data={"slug": "alpha", "name": "A", "csrf_token": acsrf})
    carol_c, ccsrf = client_for("carol")
    assert carol_c.post("/ui/projects/alpha/delete",
                        data={"csrf_token": ccsrf}).status_code == 403
    assert auth.get_project("alpha") is not None  # 삭제되지 않음


# ── MCP: JWT projects 클레임 스코프 (라이브 streamable-http) ─────────────
def _free_port():
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); p = sk.getsockname()[1]; sk.close()
    return p


@pytest.fixture()
def mcp_url(tmp_path):
    cfg = Config(); cfg.default_project = "why-hub"; cfg.approval = ApprovalConfig(enabled=False)
    svc = KnowledgeService(tmp_path, cfg)
    svc.save_document(_adr("adr-0001", "alpha"), actor="sys", project="alpha")
    svc.save_document(_adr("adr-0002", "beta"), actor="sys", project="beta")
    _, pub = keypair()
    verifier = JWTVerifier(public_key=pub, issuer="why-hub", audience="why-hub-mcp", algorithm="RS256")
    mcp = build_mcp(svc, verifier)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mcp.http_app(), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/mcp/"
    server.should_exit = True; th.join(timeout=5); svc.close()


def _call(url, token, name, args):
    async def go():
        async with Client(url, auth=token) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go()).data


def test_mcp_projects_claim_scopes_access(mcp_url):
    iss = make_issuer()
    # alpha editor member — beta 접근 불가.
    tok, _ = iss.issue(subject="u1", username="carol", is_admin=False,
                       scopes=["knowledge:read", "knowledge:submit"], projects={"alpha": "editor"})
    hits = _call(mcp_url, tok, "search_knowledge", {"query": "세션"})
    ids = {h["doc_id"] for h in hits}
    assert "adr-0001" in ids and "adr-0002" not in ids   # alpha 보임, beta 제외
    assert _call(mcp_url, tok, "get_document", {"id": "adr-0002"}) is None  # beta 차단
    # beta 에 쓰기 시도 → 권한 없음(ToolError)
    beta_doc = _adr("adr-0003", "beta")
    with pytest.raises(Exception):  # noqa: B017 - ToolError
        _call(mcp_url, tok, "save_document", {"markdown": beta_doc})
