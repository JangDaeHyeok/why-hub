"""정합성 점검·복구 — pending 저널 정리 + 본문 해시 대조 재색인. 멱등 ([Δ] §6).

파일+SQLite 이중 쓰기는 원자적이지 않다. 기동/주기 실행 시 이 루틴이 수렴시킨다:
1. `docs/` 의 각 문서가 FTS 에 최신으로 색인돼 있는지 본문 해시 대조 → 불일치면 재색인.
2. 스냅샷 없음/손상 → 현재 본문으로 스냅샷 재작성(diff 기준점 복구).
3. 파일 없는 인덱스 고아 행 제거.
4. pending 저널 정리(전체 스캔으로 정합성이 수렴하므로 저널은 힌트).

**멱등:** 여러 번 돌려도 결과 동일(두 번째 실행은 아무것도 바꾸지 않는다).

구현 Phase: P05.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import Config
from . import journal, paths, snapshots
from .index_fts import open_index
from .normalize import normalize
from .save import to_document


def run(root, config: Config | None = None, index=None) -> dict:
    config = config or Config()
    root = Path(root)
    owns_index = index is None
    if owns_index:
        index = open_index(root)

    result = {
        "reindexed": 0,
        "snapshots_written": 0,
        "orphans_removed": 0,
        "journals_cleared": 0,
        "history_rolled_back": 0,
    }
    try:
        # 0. pending 저널 복구 — 삭제 전에 steps_done 을 보고 부분 save 를 되돌린다.
        #    이력만 append 되고 문서 쓰기 전에 크래시하면(= 'history' 완료, 'doc' 미완)
        #    고아 이력이 영구히 남으므로 hist_size 로 truncate 한다(§6).
        for doc_id in journal.pending(root):
            j = journal.load(doc_id, root) or {}
            steps = j.get("steps_done", [])
            if "history" in steps and "doc" not in steps:
                _rollback_orphan_history(doc_id, root, j.get("hist_size", 0))
                result["history_rolled_back"] += 1
            journal.remove(doc_id, root)
            result["journals_cleared"] += 1

        # 1. docs/ 스캔 — 각 문서를 인덱스·스냅샷과 대조해 수렴.
        seen: set[str] = set()
        docs_dir = paths.docs_dir(root)
        if docs_dir.exists():
            for f in sorted(docs_dir.glob("*/*.md")):
                nd = normalize(f.read_text(encoding="utf-8"))
                doc_id = nd.id
                if not doc_id:
                    continue
                seen.add(doc_id)
                body_hash = _sha(nd.body)
                rel_path = str(f.relative_to(root))
                doc = to_document(nd)

                # 본문 해시 OR 메타/경로가 다르면 재색인(메타-only 변경도 수렴, C5).
                meta = index.get_meta(doc_id)
                if meta is None or meta.get("body_hash") != body_hash or _meta_stale(meta, doc, rel_path):
                    index.reindex_doc(doc, path=rel_path, body_hash=body_hash)
                    result["reindexed"] += 1

                # 스냅샷이 없거나/손상이거나/현재 본문과 다르면 재작성(C4).
                snap = snapshots.load(doc_id, root)  # 없음/손상 시 None
                if snap is None or snap != nd.body:
                    snapshots.write(doc_id, nd.body, root)
                    result["snapshots_written"] += 1

        # 2. 파일 없는 인덱스 고아 행 제거
        for doc_id in index.all_doc_ids():
            if doc_id not in seen:
                index.remove_doc(doc_id)
                result["orphans_removed"] += 1

        return result
    finally:
        if owns_index:
            index.close()


def _meta_stale(meta: dict, doc, rel_path: str) -> bool:
    """인덱스 메타가 현재 문서와 다른가(본문 외 변경 감지)."""
    return (
        meta.get("type") != doc.type
        or meta.get("status") != doc.status
        or meta.get("title") != doc.title
        or meta.get("path") != rel_path
        or (meta.get("source") or None) != (doc.source or None)
        or (meta.get("tags") or []) != (doc.tags or [])
    )


def _rollback_orphan_history(doc_id: str, root, hist_size: int) -> None:
    hp = paths.history_path(root, doc_id)
    if not hp.exists():
        return
    if hist_size <= 0:
        hp.unlink()
    else:
        with open(hp, "r+", encoding="utf-8") as f:
            f.truncate(hist_size)


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
