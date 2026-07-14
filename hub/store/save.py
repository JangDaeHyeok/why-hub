"""save 루틴 오케스트레이션 — 모든 쓰기의 단일 진입점 ([Δ] §2).

단계(반드시 이 순서): 파싱+정규화 → lint 게이트(부작용 이전) → 문서 락 → 저널 시작 →
스냅샷 로드 → diff+앵커 귀속 → change_type 확정 → 이력 append → 문서 본문 쓰기 →
스냅샷 갱신 → docs-diff 기록 → FTS 재색인 → 저널 커밋.

**불변식:** 모든 쓰기는 이 엔진을 경유하며(CLAUDE.md §2-1), lint 실패 시 저장·색인하지 않는다(§2-4).
부작용은 전부 문서 락 안에서. 실패 시 저널 근거 롤백, 되돌릴 수 없으면 pending 으로 남겨 reconcile 이 수렴.

구현 Phase: P05.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

from ..config import Config
import yaml

from ..models import Document, SaveResult
from . import diffing, history, journal, paths, snapshots
from .index_fts import open_index
from .lint import LintError, lint
from .locking import doc_lock
from .normalize import NormalizedDoc, normalize


def save_document(
    raw_markdown: str,
    *,
    root,
    actor: str,
    config: Config | None = None,
    change_type: str | None = None,
    intended_diff: str | None = None,
    index=None,
    now: str | None = None,
) -> SaveResult:
    """모든 쓰기의 단일 진입점. 반환: SaveResult. 예외: LintError / LockTimeout."""
    config = config or Config()
    root = Path(root)
    now_ts = now or _now()

    # 1. 파싱 + 정규화 — 잘못된 YAML frontmatter 는 save 게이트의 LintError 로 변환
    #    (raw 예외로 500 나지 않도록; UI/HTTP 가 422+사유로 처리).
    try:
        nd = normalize(raw_markdown, now=now_ts)
    except yaml.YAMLError as e:
        raise LintError([f"frontmatter YAML 파싱 실패: {e}"]) from e

    owns_index = index is None
    if owns_index:
        index = open_index(root)
    try:
        # 2. lint 게이트 — 부작용 이전. 실패 시 여기서 중단(파일 미변경).
        lint(nd, config, exists_fn=index.exists)
        doc_type = nd.frontmatter.get("type")

        # 3. 문서 락 — 이하 전부 락 안에서.
        with doc_lock(nd.id, root, timeout=config.lock_timeout):
            return _save_locked(
                nd, doc_type, actor, change_type, intended_diff, now_ts, root, index
            )
    finally:
        if owns_index:
            index.close()


def _save_locked(nd, doc_type, actor, change_type_arg, intended_diff, now_ts, root, index):
    doc_id = nd.id
    warnings: list[str] = []

    # 타입 불변식: 기존 문서의 type 은 바꿀 수 없다(디렉토리/경로가 type 에 종속 →
    # 변경 시 구 경로 파일이 남아 reconcile 이 stale 을 재색인). 부작용 이전에 거부.
    prev_meta = index.get_meta(doc_id)
    if prev_meta and prev_meta.get("type") and prev_meta["type"] != doc_type:
        raise LintError(
            [f"type 변경 불가: 기존 '{prev_meta['type']}' → '{doc_type}' (문서 타입은 불변)"]
        )

    doc_path = paths.doc_path(root, doc_id, doc_type)
    hist_path = paths.history_path(root, doc_id)
    snap_p = paths.snapshot_path(root, doc_id)
    snap_h = paths.snapshot_hash_path(root, doc_id)
    rel_path = str(doc_path.relative_to(root))

    # docs-diff 대상 경로/직전 상태(롤백용)
    diff_path = paths.docs_diff_path(root, doc_id, now_ts[:10]) if intended_diff else None

    # 롤백용 직전 상태 캡처
    prev = {
        "doc": doc_path.read_text(encoding="utf-8") if doc_path.exists() else None,
        "hist_size": hist_path.stat().st_size if hist_path.exists() else 0,
        "snap": snap_p.read_text(encoding="utf-8") if snap_p.exists() else None,
        "snap_hash": snap_h.read_text(encoding="utf-8") if snap_h.exists() else None,
        "diff_path": str(diff_path) if diff_path else None,
        "diff_existed": bool(diff_path and diff_path.exists()),
        "diff_prev": (
            diff_path.read_text(encoding="utf-8")
            if diff_path and diff_path.exists() else None
        ),
    }

    # 4. 저널 시작 (hist_size 기록 → 크래시 후 reconcile 이 고아 이력 롤백에 사용)
    j = journal.begin(
        doc_id, root, op="save",
        target_paths=[str(doc_path), str(hist_path), str(snap_p)],
        hist_size=prev["hist_size"],
    )
    try:
        # 5. 스냅샷 로드 (+ 손상 감지 → 전체 created 안전 처리)
        snapshot_exists = snapshots.exists(doc_id, root)
        old = snapshots.load(doc_id, root)
        if snapshot_exists and old is None:
            warnings.append(f"스냅샷 손상: {doc_id} → 전체 created 안전 처리")

        # 6. diff + 앵커 귀속
        hunks = diffing.diff(old, nd.body)

        # 7. change_type 확정 (인자 우선, 없으면 자동 판정)
        if change_type_arg:
            ct = change_type_arg
        else:
            prev_fm = _read_prev_frontmatter(root, doc_id, index)
            ct = history.determine_change_type(
                snapshot_exists=(old is not None),
                new_status=nd.frontmatter.get("status"),
                prev_status=(prev_fm or {}).get("status"),
                new_supersedes=nd.frontmatter.get("supersedes"),
                prev_supersedes=(prev_fm or {}).get("supersedes"),
            )

        # 8. 이력 항목 생성 + append (변경 없으면 빈 목록 → append 스킵)
        entries = history.build(doc_id, hunks, actor=actor, change_type=ct, ts=now_ts)
        history_id = history.append(doc_id, entries, root)
        journal.step(doc_id, root, j, "history")

        # 9. 문서 본문 쓰기 (정규화된 최종본)
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(nd.text, encoding="utf-8")
        journal.step(doc_id, root, j, "doc")

        # 10. 스냅샷 갱신
        snapshots.write(doc_id, nd.body, root)
        journal.step(doc_id, root, j, "snapshot")

        # 11. docs-diff 기록 (의도된 변경이 있을 때)
        if intended_diff:
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            diff_path.write_text(intended_diff, encoding="utf-8")
            journal.step(doc_id, root, j, "docs-diff")

        # 12. FTS 재색인 (project 미지정 시 index.default_project 로 보정)
        index.reindex_doc(
            to_document(nd, default_project=getattr(index, "default_project", None)),
            path=rel_path, body_hash=_sha(nd.body),
        )
        journal.step(doc_id, root, j, "index")

        # 13. 저널 커밋
        journal.commit(doc_id, root)

        return SaveResult(
            id=doc_id,
            change_type=ct,
            anchors_changed=[h.anchor for h in hunks],
            history_id=history_id,
            warnings=warnings,
        )
    except Exception:
        # 롤백 시도 → 성공 시 저널 제거, 실패 시 pending 으로 남겨 reconcile 이 수렴.
        try:
            _rollback(doc_id, root, index, prev, doc_path, hist_path, snap_p, snap_h)
            journal.commit(doc_id, root)
        except Exception:
            pass
        raise


def _rollback(doc_id, root, index, prev, doc_path, hist_path, snap_p, snap_h) -> None:
    # 문서 본문
    if prev["doc"] is None:
        if doc_path.exists():
            doc_path.unlink()
    else:
        doc_path.write_text(prev["doc"], encoding="utf-8")

    # 이력 (append-only → 직전 크기로 truncate)
    if hist_path.exists():
        if prev["hist_size"] == 0:
            hist_path.unlink()
        else:
            with open(hist_path, "r+", encoding="utf-8") as f:
                f.truncate(prev["hist_size"])

    # 스냅샷
    if prev["snap"] is None:
        for p in (snap_p, snap_h):
            if p.exists():
                p.unlink()
    else:
        snap_p.write_text(prev["snap"], encoding="utf-8")
        if prev["snap_hash"] is not None:
            snap_h.write_text(prev["snap_hash"], encoding="utf-8")

    # docs-diff (있었으면 직전 내용 복원, 새로 만든 것이면 삭제)
    if prev.get("diff_path"):
        dp = Path(prev["diff_path"])
        if prev.get("diff_existed"):
            if prev.get("diff_prev") is not None:
                dp.write_text(prev["diff_prev"], encoding="utf-8")
        elif dp.exists():
            dp.unlink()

    # 인덱스 정합
    if prev["doc"] is None:
        index.remove_doc(doc_id)
    else:
        pnd = normalize(prev["doc"])
        rel = str(paths.doc_path(root, doc_id, pnd.type).relative_to(root))
        index.reindex_doc(
            to_document(pnd, default_project=getattr(index, "default_project", None)),
            path=rel, body_hash=_sha(pnd.body),
        )


def to_document(nd: NormalizedDoc, *, default_project: str | None = None) -> Document:
    """정규화 결과 → Document 값 객체 (색인용).

    project 는 frontmatter 가 원천이며, 없으면 default_project 로 보정(미지정=기본 프로젝트)."""
    fm = nd.frontmatter
    return Document(
        id=fm.get("id"),
        type=fm.get("type"),
        title=fm.get("title", ""),
        status=fm.get("status", ""),
        created=str(fm.get("created", "")),
        body=nd.body,
        updated=fm.get("updated"),
        author=fm.get("author"),
        source=fm.get("source"),
        project=fm.get("project") or default_project,
        tags=fm.get("tags") or [],
        related=fm.get("related") or [],
        supersedes=fm.get("supersedes"),
    )


def _read_prev_frontmatter(root, doc_id, index) -> dict | None:
    meta = index.get_meta(doc_id)
    if not meta or not meta.get("path"):
        return None
    p = Path(root) / meta["path"]
    if not p.exists():
        return None
    try:
        return normalize(p.read_text(encoding="utf-8")).frontmatter
    except Exception:
        return None


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")
