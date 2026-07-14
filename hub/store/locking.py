"""문서별 파일 락 — .locks/<id>.lock, fcntl.flock 배타 ([Δ] §8).

- `doc_lock(id, root, timeout)` 컨텍스트 매니저. 타임아웃 초과 시 `LockTimeout`.
- **한 save 는 자기 문서 하나만 잠근다**(다중 문서 잠금 금지 → 데드락 회피).
- 별개 open() 은 별개 open file description 이라 같은 프로세스의 다른 스레드끼리도 배타된다.

구현 Phase: P05.
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager

from . import paths


class LockTimeout(Exception):
    """타임아웃 내에 문서 락을 얻지 못함."""


@contextmanager
def doc_lock(doc_id: str, root, timeout: float = 10.0, poll: float = 0.02):
    p = paths.lock_path(root, doc_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - start >= timeout:
                    raise LockTimeout(f"문서 락 타임아웃: {doc_id}")
                time.sleep(poll)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()
