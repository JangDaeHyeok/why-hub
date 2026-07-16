"""인증 — member/admin RBAC 가 HTTP 경계에서 동일 적용 (구현스펙-인증인가-RBAC.md §2.1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hub.config import ApprovalConfig, Config
from hub.interfaces.http_api import build_app
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
    make_user(auth, "dave")
    make_user(auth, "alice", is_admin=True)
    app = build_app(svc, auth)

    def client_for(username):
        c = TestClient(app)
        tok, csrf = login(auth, username)
        return auth_client(c, auth, tok, csrf)

    yield client_for
    svc.close()
    auth.close()


def test_member_can_read_and_submit(env):
    carol = env("carol")
    assert carol.get("/docs").status_code == 200
    r = carol.put("/docs/adr-0001", json={"markdown": _ADR})
    assert r.status_code == 200 and r.json()["status"] == "pending"


def test_member_cannot_approve_or_reject(env):
    carol = env("carol")
    sub_id = carol.put("/docs/adr-0001", json={"markdown": _ADR}).json()["submission_id"]
    assert carol.post(f"/submissions/{sub_id}/approve", json={}).status_code == 403
    assert carol.post(f"/submissions/{sub_id}/reject", json={}).status_code == 403


def test_admin_can_approve(env):
    carol = env("carol")
    sub_id = carol.put("/docs/adr-0001", json={"markdown": _ADR}).json()["submission_id"]
    alice = env("alice")
    assert alice.post(f"/submissions/{sub_id}/approve", json={}).status_code == 200
    assert alice.get("/docs/adr-0001").status_code == 200


def test_member_cannot_view_others_submission(env):
    carol = env("carol")
    sub_id = carol.put("/docs/adr-0001", json={"markdown": _ADR}).json()["submission_id"]
    dave = env("dave")
    # 타인 제출 상세 → 403
    assert dave.get(f"/submissions/{sub_id}").status_code == 403
    # 본인 제출은 조회 가능
    assert carol.get(f"/submissions/{sub_id}").status_code == 200


def test_admin_sees_all_pending_member_sees_own(env):
    carol = env("carol")
    dave = env("dave")
    carol.put("/docs/adr-0001", json={"markdown": _ADR})
    dave.put("/docs/adr-0002", json={"markdown": _ADR.replace("adr-0001", "adr-0002")})
    # admin 은 전체
    alice = env("alice")
    allp = alice.get("/submissions", params={"status": "pending"}).json()
    assert {s["doc_id"] for s in allp} == {"adr-0001", "adr-0002"}
    # member 는 본인 것만
    mine = carol.get("/submissions", params={"status": "pending"}).json()
    assert {s["doc_id"] for s in mine} == {"adr-0001"}
