"""코드 리뷰 지적(C1~C12) 수정 회귀 테스트."""

from __future__ import annotations

import threading

import pytest

from hub.chat import ChatOrchestrator
from hub.config import ApprovalConfig, Config
from hub.service import KnowledgeService
from hub.store import anchors as anchors_mod
from hub.store import journal, paths, reconcile, snapshots
from hub.store.file_store import FileStore
from hub.store.index_fts import Index, open_index
from hub.store.lint import LintError, lint
from hub.store.normalize import normalize
from hub.store.save import save_document


def _adr(id="adr-0001", status="accepted", title="인증 방식",
         decision="서버 세션 방식과 Redis 를 쓴다.", alt="JWT 방식은 폐기 지연으로 기각.", dtype="adr"):
    return (
        f"---\nid: {id}\ntype: {dtype}\ntitle: {title}\nstatus: {status}\n"
        "created: 2026-06-01\ntags: [auth]\n---\n\n"
        "# 배경\n\n인증 만료 처리가 어렵다.\n\n"
        f"# 결정\n\n{decision}\n\n# 근거\n\n즉시 폐기가 가능하다.\n\n"
        f"# 대안\n\n{alt}\n\n# 결과\n\n로그인이 서버 세션에 의존한다.\n"
    )


# ── C1: 경로 traversal 차단 ───────────────────────────────────────────
def test_c1_path_separator_in_id_rejected():
    # 설정 정규식이 느슨해도(note='^.+$') 경로 구분자는 lint 가 막는다.
    cfg = Config()
    cfg.id_patterns["note"] = "^.+$"
    raw = "---\nid: n/../../evil\ntype: note\ntitle: t\nstatus: proposed\ncreated: 2026-01-01\n---\n\n# H\n\nx\n"
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), cfg)
    assert any("경로" in r for r in ei.value.reasons)


def test_c1_dotdot_id_rejected():
    raw = "---\nid: adr-..0001\ntype: adr\ntitle: t\nstatus: proposed\ncreated: 2026-01-01\n---\n\n# 배경\n\na\n# 결정\n\nb\n# 근거\n\nc\n# 대안\n\nd\n# 결과\n\ne\n"
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config())
    assert any("경로" in r for r in ei.value.reasons)


# ── C6: FTS 자유 입력 안전 처리 ───────────────────────────────────────
@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path)
    s.save_document(_adr(), actor="a", now="2026-07-14T10:00:00")
    yield s
    s.close()


@pytest.mark.parametrize("q", ['foo-bar', '"', 'OR', 'a AND b', '세션 OR', ')(', ''])
def test_c6_freetext_search_never_500(svc, q):
    # FTS 구문 특수문자로 예외가 나지 않아야 한다(빈 결과여도 OK).
    hits = svc.search_knowledge(q)
    assert isinstance(hits, list)


def test_c6_normal_query_still_works(svc):
    assert any(h["doc_id"] == "adr-0001" for h in svc.search_knowledge("세션"))
    # 하이픈 포함 토큰도 안전하게 매칭(토큰 분해)
    assert isinstance(svc.search_knowledge("세션-방식"), list)


# ── C7: ingest frontmatter YAML 직렬화 ────────────────────────────────
def test_c7_ingest_title_with_colon(svc):
    res = svc.ingest_source("notion:p1", content="# T\n\n내용.\n",
                            title="Design: tradeoffs 정리", now="2026-07-14T10:00:00")
    doc = svc.get_document(res.id)
    assert doc["title"] == "Design: tradeoffs 정리"


def test_c7_ingest_source_ref_with_special_chars(svc):
    res = svc.ingest_source("http://x/y?a=b: c", content="# T\n\nx.\n",
                            now="2026-07-14T10:00:00")
    assert svc.get_document(res.id) is not None


# ── C8: 동시 신규 ingest 원자적 채번 ──────────────────────────────────
def test_c8_concurrent_ingest_distinct_ids(tmp_path):
    svc = KnowledgeService(tmp_path)
    errors = []

    def worker(i):
        try:
            svc.ingest_source(f"src-{i}", content=f"# D{i}\n\n내용 {i}.\n")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    docs = svc.list_documents()
    ids = [d["id"] for d in docs]
    assert len(ids) == 6  # 6개 소스 → 6개 문서(덮어쓰기 없음)
    assert len(set(ids)) == 6  # id 중복 없음
    svc.close()


# ── C9: type 변경 거부 ────────────────────────────────────────────────
def test_c9_type_change_rejected(tmp_path):
    svc = KnowledgeService(tmp_path)
    svc.save_document(_adr("adr-0001", dtype="adr"), actor="a", now="2026-07-14T10:00:00")
    # 같은 id 로 type 을 guide 로 바꿔 저장 시도 → 거부
    guide = (
        "---\nid: adr-0001\ntype: guide\ntitle: t\nstatus: accepted\ncreated: 2026-06-01\n---\n\n# H\n\nx\n"
    )
    with pytest.raises(LintError) as ei:
        svc.save_document(guide, actor="a", now="2026-07-14T11:00:00")
    assert any("type 변경 불가" in r for r in ei.value.reasons)
    # 기존 adr 파일만 남고 guide 경로엔 안 생김
    assert paths.doc_path(tmp_path, "adr-0001", "adr").exists()
    assert not paths.doc_path(tmp_path, "adr-0001", "guide").exists()
    svc.close()


# ── C10: 잘못된 YAML → LintError ──────────────────────────────────────
def test_c10_malformed_yaml_becomes_lint_error(tmp_path):
    svc = KnowledgeService(tmp_path)
    bad = "---\nid: adr-0001\ntitle: [unclosed\ntype: adr\n---\n\n# 배경\n\nx\n"
    with pytest.raises(LintError) as ei:
        svc.save_document(bad, actor="a", now="2026-07-14T10:00:00")
    assert any("YAML" in r for r in ei.value.reasons)
    svc.close()


# ── C11: docs-diff 롤백 ───────────────────────────────────────────────
def test_c11_docs_diff_rolled_back_on_failure(tmp_path):
    idx = open_index(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("reindex 실패 시뮬레이션")

    idx.reindex_doc = boom  # 마지막 단계에서 실패시켜 롤백 유발
    with pytest.raises(RuntimeError):
        save_document(_adr(), root=tmp_path, actor="a",
                      intended_diff="의도: 세션 전환", index=idx, now="2026-07-14T10:00:00")
    idx.close()
    # 실패한 save 의 docs-diff 부작용이 남지 않아야 한다.
    assert not paths.docs_diff_path(tmp_path, "adr-0001", "2026-07-14").exists()
    assert not paths.doc_path(tmp_path, "adr-0001", "adr").exists()


# ── C12: limit 없는 offset ────────────────────────────────────────────
def test_c12_offset_without_limit(tmp_path):
    svc = KnowledgeService(tmp_path)
    for i in range(1, 4):
        svc.save_document(_adr(f"adr-000{i}"), actor="a", now=f"2026-07-14T10:0{i}:00")
    all_ids = [d["id"] for d in svc.list_documents()]
    assert all_ids == ["adr-0001", "adr-0002", "adr-0003"]
    # offset 만 주어도 앞부분을 건너뛴다.
    skipped = [d["id"] for d in svc.list_documents(offset=1)]
    assert skipped == ["adr-0002", "adr-0003"]
    svc.close()


# ── C3: reconcile 고아 이력 롤백 ──────────────────────────────────────
def test_c3_reconcile_rolls_back_orphan_history(tmp_path):
    save_document(_adr(), root=tmp_path, actor="a", now="2026-07-14T10:00:00")
    hp = paths.history_path(tmp_path, "adr-0001")
    size_before = hp.stat().st_size

    # 크래시 시뮬레이션: 이력만 append 되고 문서 미기록.
    with open(hp, "a", encoding="utf-8") as f:
        f.write("- ts: 2026-07-14T11:00:00\n  actor: x\n  type: revision\n  anchor: 결정\n  summary: 유령\n  delta: '+ x'\n")
    journal.begin("adr-0001", tmp_path, op="save", hist_size=size_before)
    j = journal.load("adr-0001", tmp_path)
    j["steps_done"] = ["history"]  # 'doc' 없음
    journal._write("adr-0001", tmp_path, j)

    from hub.store import history
    assert len(history.read("adr-0001", tmp_path)) == 2  # 유령 포함

    res = reconcile.run(tmp_path)
    assert res["history_rolled_back"] == 1
    assert len(history.read("adr-0001", tmp_path)) == 1  # 유령 제거됨
    assert journal.pending(tmp_path) == []


# ── C4: reconcile 스냅샷 불일치 재작성 ────────────────────────────────
def test_c4_reconcile_rebuilds_stale_snapshot(tmp_path):
    save_document(_adr(decision="v1 결정."), root=tmp_path, actor="a", now="2026-07-14T10:00:00")
    # 크래시 시뮬레이션: 문서는 v2 로 갱신됐지만 스냅샷은 v1(유효 해시) 그대로.
    v2 = normalize(_adr(decision="v2 로 완전히 바뀐 결정."))
    paths.doc_path(tmp_path, "adr-0001", "adr").write_text(v2.text, encoding="utf-8")

    assert snapshots.load("adr-0001", tmp_path) != v2.body  # stale
    res = reconcile.run(tmp_path)
    assert res["snapshots_written"] >= 1
    assert snapshots.load("adr-0001", tmp_path) == v2.body  # 재작성됨


# ── C5: reconcile 메타-only 변경 재색인 ───────────────────────────────
def test_c5_reconcile_reindexes_metadata_only_change(tmp_path):
    save_document(_adr(status="accepted"), root=tmp_path, actor="a", now="2026-07-14T10:00:00")
    # 크래시 시뮬레이션: 본문 동일, status 만 deprecated 로 바뀐 문서(색인 미반영).
    changed = normalize(_adr(status="deprecated"))
    paths.doc_path(tmp_path, "adr-0001", "adr").write_text(changed.text, encoding="utf-8")

    idx = open_index(tmp_path)
    assert idx.get_meta("adr-0001")["status"] == "accepted"  # stale
    idx.close()

    res = reconcile.run(tmp_path)
    assert res["reindexed"] >= 1

    idx = open_index(tmp_path)
    assert idx.get_meta("adr-0001")["status"] == "deprecated"  # 수렴
    idx.close()


# ══ 2차 리뷰 수정 회귀 테스트 ═══════════════════════════════════════════

# ── R1: 중첩 코드펜스(4-백틱 안의 3-백틱)를 조기에 닫지 않는다 ──────────
def test_r1_normalize_preserves_nested_fence():
    # 4-백틱 펜스 안에 3-백틱 예시가 들어 있어도, 내부의 헤더·빈 줄·공백이 재작성되면 안 된다.
    inner = "````\n# 헤더 아님\n\n\n```\n코드 예시\n```\n   들여쓴 줄\n````"
    raw = (
        "---\nid: guide-0001\ntype: guide\ntitle: t\nstatus: proposed\ncreated: 2026-01-01\n---\n\n"
        f"# H\n\n{inner}\n"
    )
    nd = normalize(raw)
    # 내부 원문(짧은 펜스·빈 줄·트레일링 공백 포함)이 그대로 보존된다.
    assert "# 헤더 아님\n\n\n```\n코드 예시\n```\n   들여쓴 줄" in nd.body
    # 멱등성 유지.
    assert normalize(nd.text).text == nd.text


def test_r1_anchor_ignores_headers_in_nested_fence():
    body = "# 진짜\n\n````\n# 가짜\n```\n# 여전히 가짜\n```\n````\n\n# 진짜2\n\nx\n"
    slugs = [a.slug for a in anchors_mod.parse_anchors(body)]
    assert slugs == ["진짜", "진짜2"]  # 펜스 안 헤더는 모두 무시


# ── R2: FTS OR 모드에서 예약어 토큰이 연산자로 오인되지 않는다 ──────────
def test_r2_fts_or_mode_reserved_tokens_no_error(svc):
    # OR/AND/NOT/NEAR 같은 FTS5 예약어가 토큰으로 와도 OperationalError 없이 리스트 반환.
    for toks in (["OR"], ["AND", "세션"], ["NOT"], ["NEAR", "OR", "AND"]):
        hits = svc.store.search(toks, None, 10, mode="or")
        assert isinstance(hits, list)
    # 정상 OR 검색은 여전히 매칭.
    assert any(h.doc_id == "adr-0001" for h in svc.store.search(["세션", "OR"], None, 10, mode="or"))


# ── R3: 같은 기준 버전에서 갈라진 두 제출 — 나중 승인은 충돌로 거부 ──────
def _approval_svc(tmp_path):
    c = Config()
    c.approval = ApprovalConfig(enabled=True, admins=["alice"])
    return KnowledgeService(tmp_path, c)


def test_r3_stale_approval_rejected_no_lost_update(tmp_path):
    svc = _approval_svc(tmp_path)
    # 문서 생성 → 승인(기준 버전 확정).
    sid0 = svc.save_document(_adr(), actor="carol")["submission_id"]
    svc.approve_submission(sid0, approver="alice", now="2026-07-14T10:00:00")

    # 같은 기준에서 두 편집을 제출(둘 다 현재 body_hash 를 base 로 캡처).
    editA = _adr(decision="A: 세션+Redis 유지, TTL 조정.")
    editB = _adr(decision="B: 완전히 다른 방향 — 토큰 도입 검토.")
    sidA = svc.save_document(editA, actor="carol")["submission_id"]
    sidB = svc.save_document(editB, actor="dave")["submission_id"]

    # A 승인 → 반영(문서 버전 이동).
    svc.approve_submission(sidA, approver="alice", now="2026-07-14T11:00:00")
    assert "TTL 조정" in svc.get_raw("adr-0001")

    # B 승인 → 기준 버전 불일치 → 충돌 거부(먼저 승인된 A 의 변경이 조용히 사라지지 않는다).
    with pytest.raises(ValueError) as ei:
        svc.approve_submission(sidB, approver="alice", now="2026-07-14T12:00:00")
    assert "충돌" in str(ei.value)
    # A 의 내용은 그대로, B 제출은 pending 유지(재작성 가능).
    assert "TTL 조정" in svc.get_raw("adr-0001")
    assert svc.get_submission(sidB)["status"] == "pending"
    svc.close()


def test_r3_idempotent_recovery_reapprove_after_crash(tmp_path):
    # P2-5: 반영은 됐는데 상태 갱신 전에 크래시 → 문서 적용됨 + 제출 pending.
    # UI 에서 재승인하면 충돌이 아니라 멱등 복구(상태만 approved)로 수렴해야 한다.
    svc = _approval_svc(tmp_path)
    sid0 = svc.save_document(_adr(), actor="carol")["submission_id"]
    svc.approve_submission(sid0, approver="alice", now="2026-07-14T10:00:00")

    edit = _adr(decision="세션 유지, TTL 만 조정.")
    sid = svc.save_document(edit, actor="carol")["submission_id"]
    # 크래시 시뮬레이션: 문서만 반영하고 제출 상태는 pending 그대로 둔다.
    svc._reflect(edit, actor="carol", now="2026-07-14T11:00:00")
    assert svc.get_submission(sid)["status"] == "pending"
    assert "TTL 만 조정" in svc.get_raw("adr-0001")

    # 재승인 → 충돌 아님(내용 이미 동일). 상태만 approved 로 마무리(멱등).
    res = svc.approve_submission(sid, approver="alice", now="2026-07-14T12:00:00")
    assert res.change_type == "noop"
    assert svc.get_submission(sid)["status"] == "approved"
    assert "TTL 만 조정" in svc.get_raw("adr-0001")
    svc.close()


def test_r3_normal_approval_still_works(tmp_path):
    # 충돌이 없으면 승인은 종전대로 반영된다(거짓 양성 없음).
    svc = _approval_svc(tmp_path)
    sid = svc.save_document(_adr(), actor="carol")["submission_id"]
    svc.approve_submission(sid, approver="alice", now="2026-07-14T10:00:00")
    assert svc.get_document("adr-0001") is not None
    svc.close()


# ── R4: propose_deprecate 의 reason 이 제출까지 전달된다 ─────────────────
def test_r4_deprecate_reason_reaches_submission(tmp_path):
    svc = KnowledgeService(tmp_path)  # 승인 게이트 off — apply 는 submit_change 직접 호출
    svc.save_document(_adr(), actor="a", now="2026-07-14T10:00:00")
    orch = ChatOrchestrator(svc)
    sid = orch.new_session(actor="a")
    sess = orch.get(sid)
    staged = orch._stage_deprecate(sess, "adr-0001", "더 이상 유효하지 않음", None)
    # staged item(반환 dict 아님)에 근거가 실려야 한다.
    assert sess["staged"][0]["intended_diff"] == "폐기 사유: 더 이상 유효하지 않음"

    subs = orch.apply(sid, actor="a")
    sub = svc.get_submission(subs[0]["submission_id"])
    assert sub["intended_diff"] == "폐기 사유: 더 이상 유효하지 않음"
    svc.close()


# ── R5: 잘못된 frontmatter YAML 로 PUT → 422(500 아님) ──────────────────
def test_r5_put_malformed_frontmatter_is_422(tmp_path):
    from fastapi.testclient import TestClient

    from hub.interfaces.http_api import build_app

    svc = KnowledgeService(tmp_path)
    client = TestClient(build_app(svc))
    bad = "---\nid: adr-0001\ntitle: [unclosed\ntype: adr\n---\n\n# 배경\n\nx\n"
    resp = client.put("/docs/adr-0001", json={"markdown": bad, "actor": "a"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "lint"
    svc.close()


# ── R6: pending 저널이 있으면 FileStore 오픈 시 reconcile 로 복구 ────────
def test_r6_file_store_open_reconciles_pending_journal(tmp_path):
    save_document(_adr(), root=tmp_path, actor="a", now="2026-07-14T10:00:00")
    hp = paths.history_path(tmp_path, "adr-0001")
    size_before = hp.stat().st_size
    # 크래시 시뮬레이션: 유령 이력 append + 'doc' 미완 저널.
    with open(hp, "a", encoding="utf-8") as f:
        f.write("- ts: 2026-07-14T11:00:00\n  actor: x\n  type: revision\n  anchor: 결정\n  summary: 유령\n  delta: '+ x'\n")
    journal.begin("adr-0001", tmp_path, op="save", hist_size=size_before)
    j = journal.load("adr-0001", tmp_path)
    j["steps_done"] = ["history"]
    journal._write("adr-0001", tmp_path, j)

    from hub.store import history
    assert len(history.read("adr-0001", tmp_path)) == 2  # 유령 포함

    # FileStore 오픈만으로 복구된다(별도 reconcile 호출 없이).
    store = FileStore(tmp_path, Config())
    assert len(history.read("adr-0001", tmp_path)) == 1  # 유령 제거
    assert journal.pending(tmp_path) == []
    store.close()
