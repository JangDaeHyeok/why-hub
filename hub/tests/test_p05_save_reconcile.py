"""P05 검증 — 락·저널·save·reconcile (통합) ([Δ] §2, §6, §8, §9).

- save 라운드트립(생성→save→검색→조회, 이력 1항목)
- lint 실패 → 저장소에 아무 부작용 없음
- change_type 자동 판정(accepted→deprecated → deprecation)
- 동시 save → 이력 중복 생성 없음 / LockTimeout
- reconcile: 정합성 수렴 + 멱등
"""

from __future__ import annotations

import threading

import pytest

from hub.config import Config
from hub.store import journal, paths, reconcile, snapshots
from hub.store.index_fts import open_index
from hub.store.lint import LintError
from hub.store.locking import LockTimeout, doc_lock
from hub.store.save import save_document


def _adr(status="accepted", decision="서버 세션 방식과 Redis 를 쓴다.", alt="JWT 방식은 폐기 지연으로 기각."):
    return (
        "---\n"
        "id: adr-0001\n"
        "type: adr\n"
        "title: 인증 방식\n"
        f"status: {status}\n"
        "created: 2026-06-01\n"
        "---\n\n"
        "# 배경\n\n인증 만료 처리가 어렵다.\n\n"
        f"# 결정\n\n{decision}\n\n"
        "# 근거\n\n즉시 폐기가 가능하다.\n\n"
        f"# 대안\n\n{alt}\n\n"
        "# 결과\n\n로그인이 서버 세션에 의존한다.\n"
    )


# ── save 라운드트립 ───────────────────────────────────────────────────
def test_save_roundtrip_create_search_read(tmp_path):
    res = save_document(_adr(), root=tmp_path, actor="alice",
                        now="2026-07-14T10:00:00")
    assert res.id == "adr-0001"
    assert res.change_type == "created"
    assert res.history_id == "2026-07-14T10:00:00"

    # 문서·스냅샷·이력 파일 생성됨
    assert paths.doc_path(tmp_path, "adr-0001", "adr").exists()
    assert snapshots.exists("adr-0001", tmp_path)

    # 검색으로 재확인
    idx = open_index(tmp_path)
    try:
        hits = idx.search("세션")
        assert any(h.doc_id == "adr-0001" for h in hits)
    finally:
        idx.close()

    # 이력 1항목(created)
    from hub.store import history
    entries = history.read("adr-0001", tmp_path)
    assert [e.type for e in entries] == ["created"]


def test_save_revision_records_anchor(tmp_path):
    save_document(_adr(), root=tmp_path, actor="alice", now="2026-07-14T10:00:00")
    res = save_document(_adr(decision="서버 세션 + Redis 로 완전히 전환한다."),
                        root=tmp_path, actor="bob", now="2026-07-14T11:00:00")
    assert res.change_type == "revision"
    assert "결정" in res.anchors_changed

    from hub.store import history
    entries = history.read("adr-0001", tmp_path)
    assert [e.type for e in entries] == ["created", "revision"]


def test_save_noop_adds_no_history(tmp_path):
    save_document(_adr(), root=tmp_path, actor="a", now="2026-07-14T10:00:00")
    res = save_document(_adr(), root=tmp_path, actor="a", now="2026-07-14T10:05:00")
    from hub.store import history
    assert len(history.read("adr-0001", tmp_path)) == 1  # 변경 없음 → 이력 추가 안 됨
    assert res.anchors_changed == []


# ── lint 실패 → 부작용 없음 ───────────────────────────────────────────
def test_lint_failure_no_side_effects(tmp_path):
    bad = _adr(alt="")  # 대안 비어 있음 → lint 실패
    with pytest.raises(LintError):
        save_document(bad, root=tmp_path, actor="a", now="2026-07-14T10:00:00")

    assert not paths.doc_path(tmp_path, "adr-0001", "adr").exists()
    assert not snapshots.exists("adr-0001", tmp_path)
    assert not paths.history_path(tmp_path, "adr-0001").exists()
    assert journal.pending(tmp_path) == []
    idx = open_index(tmp_path)
    try:
        assert idx.exists("adr-0001") is False
    finally:
        idx.close()


# ── change_type 자동 판정 ─────────────────────────────────────────────
def test_change_type_deprecation(tmp_path):
    save_document(_adr(status="accepted"), root=tmp_path, actor="a",
                  now="2026-07-14T10:00:00")
    res = save_document(_adr(status="deprecated"), root=tmp_path, actor="a",
                        now="2026-07-14T11:00:00")
    assert res.change_type == "deprecation"


# ── 동시성 ────────────────────────────────────────────────────────────
def test_concurrent_identical_saves_no_duplicate_history(tmp_path):
    save_document(_adr(), root=tmp_path, actor="a", now="2026-07-14T10:00:00")
    new_body = _adr(decision="서버 세션 + Redis 로 전환한다.")

    errors = []

    def worker(i):
        try:
            save_document(new_body, root=tmp_path, actor=f"w{i}",
                          now=f"2026-07-14T11:00:0{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    from hub.store import history
    entries = history.read("adr-0001", tmp_path)
    # created(1) + 동일 내용 revision(1). 나머지 4개는 diff 없음 → 이력 추가 안 됨.
    assert [e.type for e in entries] == ["created", "revision"]


def test_lock_timeout(tmp_path):
    with doc_lock("adr-0001", tmp_path, timeout=5.0):
        with pytest.raises(LockTimeout):
            save_document(_adr(), root=tmp_path, actor="a",
                          config=Config(lock_timeout=0.2), now="2026-07-14T10:00:00")


# ── reconcile ─────────────────────────────────────────────────────────
def test_reconcile_converges_and_is_idempotent(tmp_path):
    save_document(_adr(), root=tmp_path, actor="a", now="2026-07-14T10:00:00")

    # 인덱스를 낡게 만들고(스테일 body_hash) 미완 저널을 남긴다(크래시 흉내).
    idx = open_index(tmp_path)
    idx.conn.execute("UPDATE documents SET body_hash='STALE' WHERE id=?", ("adr-0001",))
    idx.conn.commit()
    idx.close()
    journal.begin("adr-0001", tmp_path, op="save")

    res = reconcile.run(tmp_path)
    assert res["reindexed"] == 1
    assert res["journals_cleared"] == 1
    assert journal.pending(tmp_path) == []

    # 검색 정상 동작(수렴 확인)
    idx = open_index(tmp_path)
    try:
        assert any(h.doc_id == "adr-0001" for h in idx.search("세션"))
    finally:
        idx.close()

    # 2회 멱등: 변경 없음
    res2 = reconcile.run(tmp_path)
    assert res2["reindexed"] == 0
    assert res2["snapshots_written"] == 0
    assert res2["orphans_removed"] == 0
    assert res2["journals_cleared"] == 0


def test_reconcile_removes_orphan_index_rows(tmp_path):
    save_document(_adr(), root=tmp_path, actor="a", now="2026-07-14T10:00:00")
    # 파일 없이 인덱스에만 존재하는 고아 행을 만든다.
    from hub.models import Document

    idx = open_index(tmp_path)
    idx.reindex_doc(Document(id="ghost-0001", type="note", title="g",
                             status="proposed", created="2026-01-01",
                             body="# H\n\nx\n"))
    idx.close()

    res = reconcile.run(tmp_path)
    assert res["orphans_removed"] == 1

    idx = open_index(tmp_path)
    try:
        assert idx.exists("ghost-0001") is False
        assert idx.exists("adr-0001") is True
    finally:
        idx.close()
