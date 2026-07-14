"""멀티프로젝트 (단일 DB + project 컬럼 스코프) 검증 (구현스펙-멀티프로젝트.md).

- 기본 프로젝트 coercion(frontmatter 무변경) · 비기본 project frontmatter 주입
- 프로젝트 간 격리(검색/목록), list_projects, 마이그레이션(NULL→default)
- normalize 멱등·lint 통과, ingest 프로젝트별 멱등, 승인 제출 project, 채팅 세션 스코프
"""

from __future__ import annotations

import tempfile

import pytest

from hub.config import ApprovalConfig, Config
from hub.service import KnowledgeService
from hub.store.index_fts import open_index
from hub.store.lint import lint
from hub.store.normalize import normalize
from hub.store.save import to_document


def _adr(id="adr-0001", *, project=None, title="결정", status="accepted", alt="JWT 기각."):
    pline = f"project: {project}\n" if project else ""
    return (
        f"---\nid: {id}\ntype: adr\ntitle: {title}\nstatus: {status}\n{pline}"
        "created: 2026-06-01\n---\n\n"
        f"# 배경\n\n맥락 {title}\n\n# 결정\n\n세션 방식\n\n# 근거\n\n이유\n\n"
        f"# 대안\n\n{alt}\n\n# 결과\n\n영향\n"
    )


def _cfg(default_project="why-hub", *, approval=False, admins=("alice",)):
    c = Config()
    c.default_project = default_project
    if approval:
        c.approval = ApprovalConfig(enabled=True, admins=list(admins))
    return c


@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path, _cfg())
    yield s
    s.close()


# ── coercion / 주입 ───────────────────────────────────────────────────
def test_default_project_coercion_no_frontmatter_change(svc):
    svc.save_document(_adr("adr-0001"), actor="a")  # project 미지정
    raw = svc.get_raw("adr-0001")
    assert "project:" not in raw  # 기본 프로젝트는 frontmatter 를 건드리지 않음
    assert svc.get_document("adr-0001")["project"] == "why-hub"  # 인덱스는 default 로 조회
    assert [d["id"] for d in svc.list_documents(project="why-hub")] == ["adr-0001"]


def test_explicit_project_injected(svc):
    svc.save_document(_adr("adr-0002"), actor="a", project="alpha")
    raw = svc.get_raw("adr-0002")
    assert "project: alpha" in raw  # 비기본은 파일에 기록(reconcile 안전)
    assert svc.get_document("adr-0002")["project"] == "alpha"


# ── 격리 ──────────────────────────────────────────────────────────────
def test_project_isolation_search_and_list(svc):
    svc.save_document(_adr("adr-0001", title="기본"), actor="a")
    svc.save_document(_adr("adr-0002", title="알파"), actor="a", project="alpha")
    svc.save_document(_adr("adr-0003", title="베타"), actor="a", project="beta")

    assert [d["id"] for d in svc.list_documents(project="alpha")] == ["adr-0002"]
    assert [d["id"] for d in svc.list_documents(project="beta")] == ["adr-0003"]
    # 전체(project=None)는 모두.
    assert len(svc.list_documents()) == 3

    assert [h["doc_id"] for h in svc.search_knowledge("세션", project="alpha")] == ["adr-0002"]
    assert [h["doc_id"] for h in svc.search_knowledge("세션", project="beta")] == ["adr-0003"]
    assert len(svc.search_knowledge("세션")) == 3  # 스코프 없으면 전체


def test_list_projects(svc):
    svc.save_document(_adr("adr-0001"), actor="a")  # why-hub
    svc.save_document(_adr("adr-0002"), actor="a", project="alpha")
    assert svc.list_projects() == ["alpha", "why-hub"]


# ── 마이그레이션 (NULL → default) ─────────────────────────────────────
def test_open_index_backfills_null_project(tmp_path):
    # default_project 없이 색인 → project NULL 로 저장.
    idx = open_index(tmp_path)
    idx.reindex_doc(to_document(normalize(_adr("adr-0001"))), path="docs/adr/adr-0001.md")
    assert idx.list_documents({"project": "why-hub"}) == []  # 아직 NULL
    idx.close()
    # default_project 로 재오픈 → 기존 NULL 행 보정.
    idx2 = open_index(tmp_path, default_project="why-hub")
    assert [d["id"] for d in idx2.list_documents({"project": "why-hub"})] == ["adr-0001"]
    assert idx2.list_projects() == ["why-hub"]
    idx2.close()


# ── normalize / lint ──────────────────────────────────────────────────
def test_normalize_idempotent_and_lint_ok_with_project():
    raw = _adr("adr-0002", project="alpha")
    once = normalize(raw).text
    twice = normalize(once).text
    assert once == twice  # 멱등
    assert "project: alpha" in once
    lint(normalize(raw), Config(), exists_fn=lambda _id: False)  # 예외 없이 통과


# ── ingest 프로젝트별 멱등 ────────────────────────────────────────────
def test_ingest_idempotent_per_project(svc):
    a1 = svc.ingest_source("src://x", content="본문 A", actor="i", project="alpha")
    a2 = svc.ingest_source("src://x", content="본문 A2", actor="i", project="alpha")
    assert a1.id == a2.id  # 같은 project·source → 갱신(멱등)
    b1 = svc.ingest_source("src://x", content="본문 B", actor="i", project="beta")
    assert b1.id != a1.id  # 다른 project → 별도 문서
    assert svc.get_document(a1.id)["project"] == "alpha"
    assert svc.get_document(b1.id)["project"] == "beta"


# ── 승인 제출의 project ───────────────────────────────────────────────
def test_submission_carries_project_and_reflects(tmp_path):
    s = KnowledgeService(tmp_path, _cfg(approval=True))
    sub = s.save_document(_adr("adr-0002"), actor="carol", project="alpha")
    assert sub["project"] == "alpha"
    # project 필터로 승인함 조회.
    assert [x["id"] for x in s.list_submissions("pending", project="alpha")] == [sub["submission_id"]]
    assert s.list_submissions("pending", project="beta") == []
    s.approve_submission(sub["submission_id"], approver="alice")
    assert s.get_document("adr-0002")["project"] == "alpha"
    assert [d["id"] for d in s.list_documents(project="alpha")] == ["adr-0002"]
    s.close()


def test_legacy_submission_none_project_scoped_as_default(tmp_path):
    # project 없이 만들어진 레거시 제출은 기본 프로젝트 스코프에서 보여야 한다.
    s = KnowledgeService(tmp_path, _cfg(approval=True))
    from hub.store import submissions as ss
    ss.create(tmp_path, op="create", doc_id="adr-0009", raw_markdown=_adr("adr-0009"),
              intended_diff=None, actor="x", prelint={"ok": True, "reasons": []},
              now="2026-07-14T00:00:00")  # project 인자 없음 → None
    assert [x["doc_id"] for x in s.list_submissions("pending", project="why-hub")] == ["adr-0009"]
    assert s.list_submissions("pending", project="beta") == []
    s.close()


# ── 채팅 세션 스코프 ──────────────────────────────────────────────────
class _SearchLLM:
    """search_knowledge 를 한 번 부르고 종료하는 가짜 LLM."""

    available = True

    def __init__(self):
        self.n = 0

    def chat(self, messages, tools=None):
        self.n += 1
        if self.n == 1:
            return {"content": None, "tool_calls": [
                {"id": "c1", "name": "search_knowledge", "arguments": {"query": "세션"}}]}
        return {"content": "확인했습니다.", "tool_calls": []}


def test_chat_search_tool_scoped_to_session_project(svc):
    svc.save_document(_adr("adr-0002", title="알파"), actor="a", project="alpha")
    svc.save_document(_adr("adr-0003", title="베타"), actor="a", project="beta")
    llm = _SearchLLM()
    r = svc.chat_turn(None, "인증 검토", actor="carol", project="alpha", llm=llm)
    # 세션 project=alpha → search 도구 결과에 alpha 문서만.
    sess = svc.get_session(r["session_id"])
    tool_msgs = [m["content"] for m in sess["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "adr-0002" in tool_msgs[0]
    assert "adr-0003" not in tool_msgs[0]
