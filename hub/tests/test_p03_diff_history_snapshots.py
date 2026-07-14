"""P03 검증 — diff·이력·스냅샷 ([Δ] §5.2, §5.3, §5.4, §9).

- 줄 단위 diff → 앵커 귀속(단일/다중 섹션, 삭제만, created)
- delta 가 +/- 줄로 정확히 기록, history append-only
- change_type 자동 판정(accepted→deprecated → deprecation)
- 스냅샷 해시 불일치 → 손상 처리(load None)
"""

from __future__ import annotations

from hub.store import diffing, history, snapshots

A_B_OLD = "# A\n\nalpha\n\n# B\n\nbeta\n"
A_B_NEW = "# A\n\nALPHA\n\n# B\n\nBETA\n"


# ── diff · 앵커 귀속 ──────────────────────────────────────────────────
def test_diff_created_when_old_none():
    hunks = diffing.diff(None, A_B_OLD)
    assert len(hunks) == 1
    assert hunks[0].created is True
    assert "alpha" in "\n".join(hunks[0].added)


def test_diff_single_section():
    new = "# A\n\nALPHA\n\n# B\n\nbeta\n"  # A 만 변경
    hunks = diffing.diff(A_B_OLD, new)
    assert [h.anchor for h in hunks] == ["A"]
    assert hunks[0].added == ["ALPHA"]
    assert hunks[0].removed == ["alpha"]


def test_diff_two_sections_split():
    hunks = diffing.diff(A_B_OLD, A_B_NEW)
    anchors = {h.anchor for h in hunks}
    assert anchors == {"A", "B"}
    by = {h.anchor: h for h in hunks}
    assert by["A"].added == ["ALPHA"] and by["A"].removed == ["alpha"]
    assert by["B"].added == ["BETA"] and by["B"].removed == ["beta"]


def test_diff_delete_only_attributes_to_enclosing_header():
    old = "# A\n\na\n\n# B\n\nb\nextra\n"
    new = "# A\n\na\n\n# B\n\nb\n"
    hunks = diffing.diff(old, new)
    assert [h.anchor for h in hunks] == ["B"]
    assert hunks[0].removed == ["extra"]
    assert hunks[0].added == []


# ── 이력 · delta ──────────────────────────────────────────────────────
def test_history_delta_plus_minus_format():
    hunks = diffing.diff(A_B_OLD, "# A\n\nALPHA\n\n# B\n\nbeta\n")
    entries = history.build("adr-0001", hunks, actor="alice",
                            change_type="revision", ts="2026-07-14T10:00:00")
    assert len(entries) == 1
    e = entries[0]
    assert e.type == "revision" and e.anchor == "A"
    assert "- alpha" in e.delta and "+ ALPHA" in e.delta
    # git diff 스타일: 삭제(-)가 추가(+)보다 먼저.
    assert e.delta.index("- alpha") < e.delta.index("+ ALPHA")


def test_history_created_single_entry():
    hunks = diffing.diff(None, A_B_OLD)
    entries = history.build("adr-0001", hunks, actor="alice",
                            change_type="created", ts="2026-07-14T10:00:00")
    assert len(entries) == 1 and entries[0].type == "created"
    assert entries[0].delta.startswith("+ ")


def test_history_two_section_entries():
    hunks = diffing.diff(A_B_OLD, A_B_NEW)
    entries = history.build("adr-0001", hunks, actor="bob",
                            change_type="revision", ts="2026-07-14T10:00:00")
    assert {e.anchor for e in entries} == {"A", "B"}
    assert all(e.type == "revision" for e in entries)


# ── append-only ───────────────────────────────────────────────────────
def test_history_append_only(tmp_path):
    e1 = history.build("adr-0001", diffing.diff(None, A_B_OLD), actor="a",
                       change_type="created", ts="2026-07-14T10:00:00")
    hid = history.append("adr-0001", e1, tmp_path)
    assert hid == "2026-07-14T10:00:00"

    e2 = history.build("adr-0001", diffing.diff(A_B_OLD, A_B_NEW), actor="b",
                       change_type="revision", ts="2026-07-14T11:00:00")
    history.append("adr-0001", e2, tmp_path)

    entries = history.read("adr-0001", tmp_path)
    # created(1) + revision(2) = 3, 순서 보존, 첫 항목 불변.
    assert [e.type for e in entries] == ["created", "revision", "revision"]
    assert entries[0].actor == "a" and entries[0].ts == "2026-07-14T10:00:00"


# ── change_type 자동 판정 ─────────────────────────────────────────────
def test_change_type_created_when_no_snapshot():
    assert history.determine_change_type(snapshot_exists=False) == "created"


def test_change_type_deprecation_on_accepted_to_deprecated():
    ct = history.determine_change_type(
        snapshot_exists=True, prev_status="accepted", new_status="deprecated"
    )
    assert ct == "deprecation"


def test_change_type_supersede_on_new_supersedes():
    ct = history.determine_change_type(
        snapshot_exists=True, prev_supersedes=None, new_supersedes="adr-0002"
    )
    assert ct == "supersede"


def test_change_type_ingest_and_default_revision():
    assert history.determine_change_type(snapshot_exists=True, via_ingest=True) == "ingest"
    assert history.determine_change_type(snapshot_exists=True) == "revision"


# ── 스냅샷 ────────────────────────────────────────────────────────────
def test_snapshot_roundtrip(tmp_path):
    snapshots.write("adr-0001", A_B_OLD, tmp_path)
    assert snapshots.exists("adr-0001", tmp_path)
    assert snapshots.load("adr-0001", tmp_path) == A_B_OLD
    assert snapshots.hash("adr-0001", tmp_path) is not None


def test_snapshot_missing_returns_none(tmp_path):
    assert snapshots.load("nope-0001", tmp_path) is None
    assert snapshots.is_corrupt("nope-0001", tmp_path) is False


def test_snapshot_hash_mismatch_treated_as_corrupt(tmp_path):
    from hub.store import paths

    snapshots.write("adr-0001", A_B_OLD, tmp_path)
    # 본문만 변조하고 해시는 그대로 → 손상.
    paths.snapshot_path(tmp_path, "adr-0001").write_text("변조됨\n", encoding="utf-8")
    assert snapshots.is_corrupt("adr-0001", tmp_path) is True
    assert snapshots.load("adr-0001", tmp_path) is None
    # 손상 스냅샷은 diff 에서 old=None(전체 created)로 안전 처리된다.
    hunks = diffing.diff(snapshots.load("adr-0001", tmp_path), A_B_NEW)
    assert hunks[0].created is True
