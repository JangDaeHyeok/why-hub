"""P04 검증 — FTS5 색인·검색 ([Δ] §7, CLAUDE.md §2-6 필터 선행).

- reindex 후 chunks_fts 가 앵커별 섹션으로 갱신(중복 없음)
- search: 필터 선행 → MATCH → bm25 정렬, 결과에 anchor 포함
- 필터로 배제된 문서 섹션은 절대 반환 안 됨(네거티브)
- project/tenant 컬럼 NULL 로 미리 개설, exists 조회
"""

from __future__ import annotations

import pytest

from hub.models import Document
from hub.store.index_fts import Index


def _doc(id, type="adr", status="accepted", title="t", tags=None, body=""):
    return Document(id=id, type=type, title=title, status=status,
                    created="2026-01-01", body=body, updated="2026-07-14",
                    tags=tags or [])


# 주의: unicode61 토크나이저는 한국어 조사를 분리하지 못한다([Δ] §7, 커스텀 토크나이저는 후속 과제).
# 그래서 질의어("세션")가 독립 토큰이 되도록 본문을 구성한다.
AUTH_BODY = "# 결정\n\n서버 세션 방식과 Redis 를 인증에 쓴다.\n\n# 대안\n\nJWT 토큰 방식은 폐기 지연으로 기각.\n"
CACHE_BODY = "# 결정\n\n캐시 계층으로 Redis 를 도입한다.\n\n# 대안\n\nMemcached 는 자료구조 부족으로 기각.\n"


@pytest.fixture()
def idx(tmp_path):
    ix = Index(tmp_path / "index.sqlite")
    yield ix
    ix.close()


# ── 색인 ──────────────────────────────────────────────────────────────
def test_reindex_creates_per_anchor_chunks(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    rows = idx.conn.execute(
        "SELECT anchor FROM chunks_fts WHERE doc_id=? ORDER BY anchor", ("adr-0001",)
    ).fetchall()
    assert sorted(r[0] for r in rows) == ["결정", "대안"]


def test_reindex_is_idempotent_no_duplicates(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))  # 재색인
    n = idx.conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE doc_id=?", ("adr-0001",)
    ).fetchone()[0]
    assert n == 2  # 앵커 2개, 중복 없음
    # documents 도 1행(upsert)
    assert idx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_project_tenant_columns_null(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    row = idx.conn.execute(
        "SELECT project, tenant FROM documents WHERE id=?", ("adr-0001",)
    ).fetchone()
    assert row == (None, None)


def test_body_hash_recorded(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    assert idx.body_hash("adr-0001") is not None


# ── 검색 ──────────────────────────────────────────────────────────────
def test_search_returns_hits_with_anchor(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    hits = idx.search("세션")
    assert hits
    assert hits[0].anchor == "결정"
    assert hits[0].doc_id == "adr-0001"
    assert "세션" in hits[0].text


def test_search_bm25_ordering(idx):
    # 'Redis' 를 두 번 언급하는 섹션이 상위로.
    idx.reindex_doc(_doc("adr-0001", body="# 결정\n\nRedis Redis Redis 다.\n"))
    idx.reindex_doc(_doc("adr-0002", body="# 결정\n\nRedis 한 번.\n"))
    hits = idx.search("Redis")
    assert [h.doc_id for h in hits][:2] == ["adr-0001", "adr-0002"]


# ── 필터 선행 (CLAUDE.md §2-6) ────────────────────────────────────────
def test_filter_by_type_precedes_search(idx):
    idx.reindex_doc(_doc("adr-0001", type="adr", body=AUTH_BODY))
    idx.reindex_doc(_doc("guide-0001", type="guide", body=CACHE_BODY))
    hits = idx.search("Redis", filters={"type": "adr"})
    assert {h.doc_id for h in hits} == {"adr-0001"}


def test_filter_excluded_doc_never_returned(idx):
    # 네거티브: status 필터로 배제된 문서의 섹션은 절대 안 나온다.
    idx.reindex_doc(_doc("adr-0001", status="accepted", body=AUTH_BODY))
    idx.reindex_doc(_doc("adr-0002", status="deprecated", body=AUTH_BODY))
    hits = idx.search("세션", filters={"status": "accepted"})
    assert all(h.doc_id == "adr-0001" for h in hits)
    assert hits  # 그래도 결과는 있음


def test_filter_by_tags(idx):
    idx.reindex_doc(_doc("adr-0001", tags=["auth", "security"], body=AUTH_BODY))
    idx.reindex_doc(_doc("adr-0002", tags=["cache"], body=CACHE_BODY))
    hits = idx.search("Redis", filters={"tags": ["auth"]})
    assert {h.doc_id for h in hits} == {"adr-0001"}


def test_filter_no_candidates_returns_empty(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    assert idx.search("세션", filters={"type": "note"}) == []


# ── exists (P02 exists_fn 연결용) ─────────────────────────────────────
def test_exists_lookup(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    assert idx.exists("adr-0001") is True
    assert idx.exists("adr-9999") is False


def test_all_doc_ids(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    idx.reindex_doc(_doc("adr-0002", body=CACHE_BODY))
    assert sorted(idx.all_doc_ids()) == ["adr-0001", "adr-0002"]


def test_remove_doc(idx):
    idx.reindex_doc(_doc("adr-0001", body=AUTH_BODY))
    idx.remove_doc("adr-0001")
    assert idx.exists("adr-0001") is False
    assert idx.conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE doc_id=?", ("adr-0001",)
    ).fetchone()[0] == 0
