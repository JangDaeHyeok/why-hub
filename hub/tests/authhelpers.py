"""테스트용 인증 헬퍼 — Principal·AuthService·로그인 쿠키 (구현스펙-인증인가-RBAC.md §10).

기존 테스트가 service 쓰기/리뷰를 Principal 로 호출하고, HTTP/UI/MCP 인증을 손쉽게 구성하도록 돕는다.
RSA 키페어는 모듈 캐시(테스트 속도 — argon2/RSA 생성 비용 절감).
"""

from __future__ import annotations

import datetime
import os

from hub.auth.jwt_service import JwtIssuer, generate_rsa_keypair
from hub.auth.principal import Principal
from hub.auth.repository import SQLiteAuthRepository
from hub.auth.service import AuthService
from hub.config import AuthConfig

_KEYPAIR: tuple[str, str] | None = None


def keypair() -> tuple[str, str]:
    global _KEYPAIR
    if _KEYPAIR is None:
        _KEYPAIR = generate_rsa_keypair()
    return _KEYPAIR


def member(username: str = "carol") -> Principal:
    return Principal.for_user(username, is_admin=False)


def admin(username: str = "alice") -> Principal:
    return Principal.for_user(username, is_admin=True)


def make_issuer(config: AuthConfig | None = None) -> JwtIssuer:
    ac = config or AuthConfig(enabled=True)
    priv, pub = keypair()
    return JwtIssuer(
        private_key_pem=priv, public_key_pem=pub, issuer=ac.issuer,
        audience=ac.mcp_audience, ttl_seconds=ac.access_token_ttl_seconds,
    )


def make_auth_service(root, *, cookie_secure: bool = False, signup: bool = True) -> AuthService:
    ac = AuthConfig(enabled=True, cookie_secure=cookie_secure, signup_enabled=signup)
    repo = SQLiteAuthRepository(os.path.join(str(root), "auth.sqlite"))
    return AuthService(
        repo, ac, pat_pepper="test-pepper", session_secret="test-session-secret",
        issuer=make_issuer(ac),
    )


def make_user(auth: AuthService, username: str, password: str = "password123",
              *, is_admin: bool = False) -> dict:
    user = auth.signup(username, password)
    if is_admin:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        auth.repo.set_user_admin(user["id"], True, now)
        user = auth.repo.get_user_by_id(user["id"])
    return user


def login(auth: AuthService, username: str, password: str = "password123") -> tuple[str, str]:
    """(session_token, csrf_token) — 인증 클라이언트 구성용."""
    token, session, _principal = auth.login(username, password)
    return token, session["csrf_token"]


def auth_client(client, auth: AuthService, token: str, csrf: str):
    """TestClient 에 세션 쿠키·CSRF 헤더를 심어 반환(상태변경 요청용)."""
    client.cookies.set(auth.config.cookie_name, token)
    client.headers.update({"X-CSRF-Token": csrf})
    return client
