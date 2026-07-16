"""간단 in-memory rate limit — 로그인·PAT 교환 (구현스펙-인증인가-RBAC.md §4).

단일 admin 컨테이너 기준으로 충분. 슬라이딩 윈도우(최근 window 초 내 시도 수). 분산 배포 시
DB/Redis 백업은 후속. 시각은 주입 가능(테스트 결정성)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, *, max_attempts: int = 10, window_seconds: float = 60.0):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, now: float | None = None) -> bool:
        """key(예: 'login:<ip>') 가 한도 내면 시도를 기록하고 True, 초과면 False(기록 안 함)."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            dq = self._hits[key]
            cutoff = t - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_attempts:
                return False
            dq.append(t)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
