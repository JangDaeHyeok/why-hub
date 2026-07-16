"""인증 — actor/approver 위장 방지 (구현스펙-인증인가-RBAC.md §2-2).

요청 바디의 actor 는 무시되고, 기록되는 actor/reviewer 는 인증 주체에서만 나온다.
MCP 도구에는 actor/approver 인자가 존재하지 않는다.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from hub.config import ApprovalConfig, Config
from hub.interfaces.http_api import build_app
from hub.interfaces.mcp_server import build_mcp
from hub.service import KnowledgeService
from hub.tests.authhelpers import auth_client, login, make_auth_service, make_user

_ADR = ("---\nid: adr-0001\ntype: adr\ntitle: t\nstatus: accepted\ncreated: 2026-06-01\n---\n\n"
        "# 배경\nx\n# 결정\ny\n# 근거\nz\n# 대안\na\n# 결과\nb\n")


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.approval = ApprovalConfig(enabled=True)
    svc = KnowledgeService(tmp_path, cfg)
    auth = make_auth_service(tmp_path)
    make_user(auth, "carol")
    make_user(auth, "alice", is_admin=True)
    app = build_app(svc, auth)

    def client_for(username):
        c = TestClient(app)
        tok, csrf = login(auth, username)
        return auth_client(c, auth, tok, csrf)

    yield svc, client_for
    svc.close()
    auth.close()


def test_body_actor_is_ignored_actor_is_session_user(env):
    svc, client_for = env
    carol = client_for("carol")
    # 바디에 actor="eve" 를 실어도 무시되고 세션 사용자(carol)로 기록.
    r = carol.put("/docs/adr-0001", json={"markdown": _ADR, "actor": "eve"})
    assert r.status_code == 200
    sub_id = r.json()["submission_id"]
    assert svc.get_submission(sub_id)["actor"] == "carol"

    # 승인 후 history actor 도 인증된 원제출자(carol) — eve 아님.
    alice = client_for("alice")
    alice.post(f"/submissions/{sub_id}/approve", json={"note": "", "approver": "eve"})
    hist = svc.get_history("adr-0001")
    assert hist[0]["actor"] == "carol"
    assert all(e["actor"] != "eve" for e in hist)
    # reviewer 도 인증 사용자(alice).
    assert svc.get_submission(sub_id)["reviewer"] == "alice"


def test_mcp_tools_have_no_actor_or_approver_args(tmp_path):
    svc = KnowledgeService(tmp_path)
    mcp = build_mcp(svc)  # 무인증 모드로도 스키마는 동일

    async def schemas():
        async with Client(mcp) as c:
            return {t.name: (t.inputSchema or {}).get("properties", {}) for t in await c.list_tools()}

    props = asyncio.run(schemas())
    assert "actor" not in props["save_document"]
    assert "actor" not in props["ingest_source"]
    assert "approver" not in props["approve_submission"]
    assert "approver" not in props["reject_submission"]
    svc.close()
