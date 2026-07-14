"""스토어 계약 테스트 — FileStore(항상) + PostgresStore(WHYHUB_TEST_DSN 있을 때만).

두 백엔드가 서비스 관점에서 동일하게 동작함을 보증한다: save 라운드트립·검색·프로젝트 격리·이력·
ingest 멱등·승인 워크플로우. PostgresStore 는 DSN 미설정 시 skip(로컬 개발/CI 마찰 없음).

Postgres 로 돌리려면:  WHYHUB_TEST_DSN=postgresql://hub:pw@localhost:5432/knowledge_hub pytest hub/tests/test_store_contract.py
"""

from __future__ import annotations

import os

import pytest

from hub.config import ApprovalConfig, Config
from hub.service import KnowledgeService

DSN = os.environ.get("WHYHUB_TEST_DSN")

BACKENDS = [
    "file",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not DSN, reason="WHYHUB_TEST_DSN 미설정 → Postgres 계약 테스트 skip"),
    ),
]

_PG_TABLES = "documents, snapshots, history, docs_diff, chunks, submissions"


def _adr(id="adr-0001", *, project=None, title="결정", status="accepted", alt="JWT 기각."):
    pline = f"project: {project}\n" if project else ""
    return (
        f"---\nid: {id}\ntype: adr\ntitle: {title}\nstatus: {status}\n{pline}"
        "created: 2026-06-01\n---\n\n"
        f"# 배경\n\n맥락 {title}\n\n# 결정\n\n세션 방식\n\n# 근거\n\n이유\n\n"
        f"# 대안\n\n{alt}\n\n# 결과\n\n영향\n"
    )


@pytest.fixture(params=BACKENDS)
def make_svc(request, tmp_path):
    """백엔드 파라미터라이즈 서비스 팩토리. postgres 는 매 테스트 스키마 truncate 로 격리."""
    created: list[KnowledgeService] = []

    def _make(*, approval=False, admins=("alice",)):
        cfg = Config()
        cfg.default_project = "why-hub"
        if approval:
            cfg.approval = ApprovalConfig(enabled=True, admins=list(admins))
        if request.param == "postgres":
            cfg.storage = "postgres"
            cfg.postgres.dsn = DSN
        s = KnowledgeService(tmp_path, cfg)
        if request.param == "postgres":
            with s.store._lock, s.store.conn.cursor() as cur:
                cur.execute(f"TRUNCATE {_PG_TABLES}")
        created.append(s)
        return s

    yield _make
    for s in created:
        s.close()


def test_save_search_roundtrip(make_svc):
    svc = make_svc()
    res = svc.save_document(_adr(), actor="alice")
    assert res.id == "adr-0001" and res.change_type == "created"
    assert svc.get_document("adr-0001")["title"] == "결정"
    assert any(h["doc_id"] == "adr-0001" for h in svc.search_knowledge("세션"))
    assert [e["type"] for e in svc.get_history("adr-0001")] == ["created"]


def test_revision_records_history(make_svc):
    svc = make_svc()
    svc.save_document(_adr(), actor="a")
    svc.save_document(_adr(alt="세션 쿠키 방식은 폐기."), actor="b")
    assert [e["type"] for e in svc.get_history("adr-0001")] == ["created", "revision"]


def test_project_isolation(make_svc):
    svc = make_svc()
    svc.save_document(_adr("adr-0001", title="기본"), actor="a")
    svc.save_document(_adr("adr-0002", title="알파"), actor="a", project="alpha")
    assert [d["id"] for d in svc.list_documents(project="alpha")] == ["adr-0002"]
    assert [h["doc_id"] for h in svc.search_knowledge("세션", project="alpha")] == ["adr-0002"]
    assert [h["doc_id"] for h in svc.search_knowledge("세션", project="why-hub")] == ["adr-0001"]
    assert sorted(svc.list_projects()) == ["alpha", "why-hub"]


def test_ingest_idempotent(make_svc):
    svc = make_svc()
    a1 = svc.ingest_source("src://x", content="본문", actor="i", project="alpha")
    a2 = svc.ingest_source("src://x", content="본문 갱신", actor="i", project="alpha")
    assert a1.id == a2.id
    b = svc.ingest_source("src://x", content="다른 프로젝트", actor="i", project="beta")
    assert b.id != a1.id


def test_search_freetext_never_raises(make_svc):
    svc = make_svc()
    svc.save_document(_adr(), actor="a")
    for q in ["foo-bar", '"', "OR", "a AND b", "세션 OR", ")(", ""]:
        assert isinstance(svc.search_knowledge(q), list)


def test_approval_flow(make_svc):
    svc = make_svc(approval=True)
    sub = svc.save_document(_adr(), actor="carol")
    assert sub["status"] == "pending"
    assert svc.get_document("adr-0001") is None  # 승인 전 미반영
    with pytest.raises(PermissionError):
        svc.approve_submission(sub["submission_id"], approver="carol")
    svc.approve_submission(sub["submission_id"], approver="alice")
    assert svc.get_document("adr-0001") is not None
    assert any(h["doc_id"] == "adr-0001" for h in svc.search_knowledge("세션"))
