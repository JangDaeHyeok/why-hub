"""인증 인터페이스 헬퍼 — 세션 principal 해석 · CSRF (구현스펙-인증인가-RBAC.md §3).

HTTP(JSON)·UI(HTML) 가 공유하는 순수 헬퍼. 401 처리(JSON 401 vs 로그인 redirect)는 각 인터페이스가
자신의 방식으로 한다. auth 비활성(로컬/테스트)일 때는 전권 로컬 주체를 쓴다.
"""

from __future__ import annotations

from .principal import ALL_SCOPES, Principal
from .tokens import csrf_equal

# auth 비활성(AUTH_ENABLED=false / 테스트) 모드의 기본 주체 — 전권 로컬 사용자.
LOCAL_PRINCIPAL = Principal(
    user_id="local", username="local", is_admin=True, scopes=ALL_SCOPES
)

# CSRF 검증에서 제외하는 안전 메서드.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def bearer_token(request) -> str | None:
    """Authorization: Bearer <token> 추출(없으면 None)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def resolve_session(auth_service, request, cookie_name: str):
    """세션 쿠키 → (session, principal) | None. auth_service None(비활성)이면 (None, LOCAL_PRINCIPAL)."""
    if auth_service is None:
        return None, LOCAL_PRINCIPAL
    got = auth_service.validate_session(request.cookies.get(cookie_name))
    if got is None:
        return None, None
    return got  # (session, principal)


def csrf_ok(session: dict | None, provided: str | None) -> bool:
    """세션의 csrf_token 과 제출값 일치 여부(상수시간). 세션 없으면 False."""
    if session is None:
        return False
    return csrf_equal(session.get("csrf_token"), provided)
