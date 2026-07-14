"""스냅샷 — load/write/hash(sha256). 해시 불일치 시 전체 created 안전 처리 ([Δ] §5.4).

- 스냅샷은 **정규화된 본문**(body) 을 저장 = 다음 저장의 diff 기준점.
- 해시(sha256)를 옆 파일에 기록. `load` 시 해시 불일치면 **손상**으로 간주 → None 반환
  (save 가 이를 "전체 created" 로 안전 처리하고 warnings 에 남긴다, §2-6).

구현 Phase: P03.
"""

from __future__ import annotations

import hashlib

from . import paths


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def write(doc_id: str, body: str, root) -> None:
    p = paths.snapshot_path(root, doc_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    paths.snapshot_hash_path(root, doc_id).write_text(_sha(body), encoding="utf-8")


def exists(doc_id: str, root) -> bool:
    return paths.snapshot_path(root, doc_id).exists()


def hash(doc_id: str, root) -> str | None:
    """저장된 스냅샷 해시(hex) 또는 None."""
    hp = paths.snapshot_hash_path(root, doc_id)
    return hp.read_text(encoding="utf-8").strip() if hp.exists() else None


def is_corrupt(doc_id: str, root) -> bool:
    """스냅샷 파일이 있으나 해시가 없거나 불일치하면 손상."""
    p = paths.snapshot_path(root, doc_id)
    if not p.exists():
        return False
    stored = hash(doc_id, root)
    if stored is None:
        return True  # 해시 없음 → 검증 불가 → 손상 취급
    return _sha(p.read_text(encoding="utf-8")) != stored


def load(doc_id: str, root) -> str | None:
    """정상 스냅샷 본문. 없음/손상 시 None(= diff 가 전체 created 로 처리)."""
    p = paths.snapshot_path(root, doc_id)
    if not p.exists():
        return None
    body = p.read_text(encoding="utf-8")
    stored = hash(doc_id, root)
    if stored is None or _sha(body) != stored:
        return None
    return body
