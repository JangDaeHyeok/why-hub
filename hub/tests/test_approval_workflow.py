"""관리자 승인 워크플로우 검증 (구현스펙-승인워크플로우.md).

- 모든 쓰기가 승인 대기 큐로 → 승인 전 검색/목록/조회에 미노출
- 비관리자 승인 차단(PermissionError / HTTP 403), 관리자 승인 시 실제 반영·색인
- 반려는 영구 미반영, 폐기(deprecate) 승인 시 status 변경
- 승인 시 정식 lint 실패 → 반영 안 됨 + 제출 pending 유지
- enabled=False 는 기존처럼 즉시 반영(하위호환)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hub.config import ApprovalConfig, Config
from hub.interfaces.http_api import build_app
from hub.service import KnowledgeService


def _adr(id="adr-0001", status="accepted", alt="JWT 방식은 폐기 지연으로 기각.", title="인증 방식"):
    return (
        f"---\nid: {id}\ntype: adr\ntitle: {title}\nstatus: {status}\n"
        "created: 2026-06-01\ntags: [auth]\n---\n\n"
        "# 배경\n\n인증 만료 처리가 어렵다.\n\n"
        "# 결정\n\n서버 세션 방식과 Redis 를 쓴다.\n\n"
        "# 근거\n\n즉시 폐기가 가능하다.\n\n"
        f"# 대안\n\n{alt}\n\n"
        "# 결과\n\n로그인이 서버 세션에 의존한다.\n"
    )


def _cfg(enabled=True, admins=("alice",)):
    c = Config()
    c.approval = ApprovalConfig(enabled=enabled, admins=list(admins))
    return c


@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path, _cfg())
    yield s
    s.close()


def test_submit_is_pending_and_not_reflected(svc):
    res = svc.save_document(_adr(), actor="carol")
    assert res["status"] == "pending" and res["doc_id"] == "adr-0001"
    # 승인 전엔 지식 store·인덱스에 미노출.
    assert svc.list_documents() == []
    assert svc.get_document("adr-0001") is None
    assert svc.search_knowledge("세션") == []
    assert len(svc.list_submissions("pending")) == 1


def test_non_admin_cannot_approve(svc):
    sid = svc.save_document(_adr(), actor="carol")["submission_id"]
    with pytest.raises(PermissionError):
        svc.approve_submission(sid, approver="carol")
    # 여전히 미반영.
    assert svc.get_document("adr-0001") is None
    assert svc.get_submission(sid)["status"] == "pending"


def test_admin_approve_reflects_and_indexes(svc):
    sid = svc.save_document(_adr(), actor="carol")["submission_id"]
    res = svc.approve_submission(sid, approver="alice")
    assert res.id == "adr-0001" and res.change_type == "created"
    assert svc.get_document("adr-0001") is not None
    assert any(h["doc_id"] == "adr-0001" for h in svc.search_knowledge("세션"))
    assert svc.get_submission(sid)["status"] == "approved"
    # 이력의 actor 는 원제출자(프로버넌스 보존).
    assert svc.get_history("adr-0001")[0]["actor"] == "carol"


def test_reject_never_reflects(svc):
    sid = svc.save_document(_adr(), actor="carol")["submission_id"]
    svc.reject_submission(sid, approver="alice", note="중복")
    assert svc.get_submission(sid)["status"] == "rejected"
    assert svc.get_document("adr-0001") is None
    # 이미 처리된 제출은 재승인 불가.
    with pytest.raises(ValueError):
        svc.approve_submission(sid, approver="alice")


def test_deprecate_submission_sets_status(svc):
    # 먼저 반영된 문서 하나 생성(승인 경유).
    sid = svc.save_document(_adr(), actor="carol")["submission_id"]
    svc.approve_submission(sid, approver="alice")
    # 폐기 제출 → 승인 → status=deprecated (프론트매터-only 변경이므로 body-diff 이력은 없지만
    # 문서·인덱스의 status 는 갱신된다).
    dep = svc.submit_change(_adr(status="deprecated"), actor="carol", op="deprecate")
    svc.approve_submission(dep["submission_id"], approver="alice")
    assert svc.get_document("adr-0001")["status"] == "deprecated"
    assert svc.list_documents({"status": "deprecated"})[0]["id"] == "adr-0001"


def test_approve_lint_failure_keeps_pending(svc):
    # 대안 섹션이 비어 정식 lint 실패해야 하는 초안(제출은 되지만 반영은 차단).
    sid = svc.save_document(_adr(alt=""), actor="carol")["submission_id"]
    assert svc.get_submission(sid)["prelint"]["ok"] is False
    from hub.store.lint import LintError

    with pytest.raises(LintError):
        svc.approve_submission(sid, approver="alice")
    # 아무것도 안 써졌고 제출은 그대로 대기.
    assert svc.get_document("adr-0001") is None
    assert svc.get_submission(sid)["status"] == "pending"


def test_disabled_reflects_immediately(tmp_path):
    s = KnowledgeService(tmp_path, _cfg(enabled=False))
    res = s.save_document(_adr(), actor="carol")
    assert res.id == "adr-0001"  # SaveResult(즉시 반영)
    assert s.get_document("adr-0001") is not None
    assert s.list_submissions() == []
    s.close()


# ── HTTP 인터페이스 ───────────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path):
    s = KnowledgeService(tmp_path, _cfg())
    with TestClient(build_app(s)) as c:
        yield c
    s.close()


def test_http_put_queues_then_admin_approves(client):
    r = client.put("/docs/adr-0001", json={"markdown": _adr(), "actor": "carol"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    sub_id = r.json()["submission_id"]
    # 승인 전 미노출.
    assert client.get("/docs/adr-0001").status_code == 404
    # 비관리자 승인 → 403.
    assert client.post(f"/submissions/{sub_id}/approve", json={"approver": "carol"}).status_code == 403
    # 관리자 승인 → 반영.
    ok = client.post(f"/submissions/{sub_id}/approve", json={"approver": "alice"})
    assert ok.status_code == 200 and ok.json()["change_type"] == "created"
    assert client.get("/docs/adr-0001").status_code == 200


def test_ingest_pending_ids_do_not_collide(svc):
    # 승인 전 서로 다른 source 를 ingest → 각기 다른 id 여야(대기 제출 id 예약, P1).
    a = svc.ingest_source("src://a", content="본문 A", actor="carol")
    b = svc.ingest_source("src://b", content="본문 B", actor="carol")
    assert a["doc_id"] != b["doc_id"]
    svc.approve_submission(a["submission_id"], approver="alice")
    svc.approve_submission(b["submission_id"], approver="alice")
    ids = sorted(d["id"] for d in svc.list_documents())
    assert a["doc_id"] in ids and b["doc_id"] in ids and len(ids) == 2


def test_ingest_same_source_pending_is_idempotent(svc):
    # 승인 전 같은 source 재-ingest → 같은 문서 id(대기 제출 매칭, 중복 채번 방지).
    a1 = svc.ingest_source("src://x", content="v1", actor="carol")
    a2 = svc.ingest_source("src://x", content="v2", actor="carol")
    assert a1["doc_id"] == a2["doc_id"]


def test_submit_target_id_mismatch_rejected(svc):
    from hub.store.lint import LintError
    # op=edit 이 adr-0001 을 지정했지만 markdown frontmatter 는 adr-0002 → 거부(P1).
    with pytest.raises(LintError):
        svc.submit_change(_adr(id="adr-0002"), actor="carol", op="edit", doc_id="adr-0001")


def test_http_list_and_reject(client):
    sub_id = client.put(
        "/docs/adr-0001", json={"markdown": _adr(), "actor": "carol"}
    ).json()["submission_id"]
    pend = client.get("/submissions", params={"status": "pending"}).json()
    assert [s["id"] for s in pend] == [sub_id]
    client.post(f"/submissions/{sub_id}/reject", json={"approver": "alice", "note": "중복"})
    assert client.get("/docs/adr-0001").status_code == 404
