"""save 저널 — .journal/<id>.json, crash-safe begin/commit ([Δ] §6).

진행 중인 save 의 의도와 완료 스텝을 작은 파일에 남긴다(op, id, steps_done[], target_paths[]).
크래시 후 기동/주기 실행 시 `reconcile.run()` 이 pending 저널을 근거로 정리한다.

구현 Phase: P05.
"""

from __future__ import annotations

import json

from . import paths


def begin(doc_id: str, root, *, op: str = "save", target_paths=None, hist_size: int = 0) -> dict:
    j = {
        "op": op,
        "id": doc_id,
        "steps_done": [],
        "target_paths": list(target_paths or []),
        "hist_size": hist_size,  # append 이전 이력 크기 → 고아 이력 롤백 기준(§6)
    }
    _write(doc_id, root, j)
    return j


def step(doc_id: str, root, j: dict, name: str) -> None:
    """완료된 스텝을 기록(디스크에 갱신)."""
    j["steps_done"].append(name)
    _write(doc_id, root, j)


def commit(doc_id: str, root) -> None:
    """성공(또는 롤백 완료) → 저널 제거."""
    p = paths.journal_path(root, doc_id)
    if p.exists():
        p.unlink()


def load(doc_id: str, root) -> dict | None:
    p = paths.journal_path(root, doc_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def pending(root) -> list[str]:
    """미완(pending) 저널의 doc_id 목록."""
    d = paths.journal_dir(root)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def remove(doc_id: str, root) -> None:
    commit(doc_id, root)


def _write(doc_id: str, root, j: dict) -> None:
    p = paths.journal_path(root, doc_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
