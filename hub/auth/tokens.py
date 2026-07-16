"""토큰 생성·해시 — 웹 세션 · CSRF · PAT (구현스펙-인증인가-RBAC.md §3,§5).

세션/PAT 원문은 절대 저장하지 않는다 — DB엔 HMAC(secret/pepper) 해시만. 비교는 상수시간.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# ── 웹 세션 토큰 (쿠키엔 원문, DB엔 HMAC 만) ────────────────────────────
def new_session_token() -> str:
    """충분히 긴 랜덤 세션 토큰(쿠키에 저장)."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str, secret: str) -> str:
    """세션 토큰의 HMAC-SHA256(session secret). DB 저장·조회용."""
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


# ── CSRF ────────────────────────────────────────────────────────────────
def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_equal(expected: str | None, provided: str | None) -> bool:
    """상수시간 CSRF 비교. 둘 중 하나라도 비면 False."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


# ── PAT: whp_<token_id>_<random_secret> ─────────────────────────────────
PAT_PREFIX = "whp"


def new_pat(token_id: str) -> tuple[str, str, str]:
    """(full_token, prefix, secret) 생성. full=`whp_<token_id>_<secret>`, prefix=`whp_<token_id>`.

    token_id 는 '_' 를 포함하지 않아야 한다(uuid hex). secret 은 충분한 엔트로피(§5).
    """
    secret = secrets.token_urlsafe(32)
    return f"{PAT_PREFIX}_{token_id}_{secret}", f"{PAT_PREFIX}_{token_id}", secret


def parse_pat(token: str) -> tuple[str, str] | None:
    """`whp_<token_id>_<secret>` → (token_id, secret). 형식 오류면 None."""
    parts = (token or "").split("_", 2)
    if len(parts) != 3 or parts[0] != PAT_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def hash_pat_secret(secret: str, pepper: str) -> str:
    """PAT secret 의 HMAC-SHA256(pepper). DB엔 이 해시만 저장(§5)."""
    return hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()


def pat_secret_matches(secret: str, pepper: str, stored_hash: str) -> bool:
    """제출된 secret 이 저장된 해시와 일치하는지(상수시간)."""
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_pat_secret(secret, pepper), stored_hash)
