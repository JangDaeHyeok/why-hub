"""인증 rate limit·타이밍 방어 회귀 테스트 (구현스펙-인증인가-RBAC.md §4, 코드리뷰 4·6·8).

- 로그인 클라이언트 전용 버킷(사용자명 회전 우회 차단)
- 공개 회원가입 클라이언트 rate limit(사용자명 회전 CPU/저장 소모 차단)
- 비활성 계정도 Argon2 검증을 수행해 응답 시간으로 계정 열거 불가

임계값을 작게 설정해 윈도우 내에서 즉시 초과시키므로 시계에 의존하지 않고 결정론적이다.
"""

from __future__ import annotations

import datetime

import pytest

from hub.auth.service import AuthError, AuthService, RateLimited
from hub.auth.repository import SQLiteAuthRepository
from hub.config import AuthConfig


def _auth(tmp_path, **cfg_kw) -> AuthService:
    ac = AuthConfig(enabled=True, cookie_secure=False, **cfg_kw)
    repo = SQLiteAuthRepository(str(tmp_path / "auth.sqlite"))
    return AuthService(repo, ac, pat_pepper="p", session_secret="s")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def test_login_client_bucket_blocks_username_rotation(tmp_path):
    # 계정 버킷은 사용자명이 매번 달라 걸리지 않지만, 클라이언트 버킷이 우회를 막는다.
    auth = _auth(tmp_path, login_max_per_account=100, login_max_per_client=3)
    # 각기 다른 사용자명으로 같은 클라이언트에서 시도 → 클라이언트 버킷 소진.
    for i in range(3):
        with pytest.raises(AuthError):
            auth.login(f"ghost{i}", "whatever", client_key="10.0.0.9")
    with pytest.raises(RateLimited):
        auth.login("ghost-final", "whatever", client_key="10.0.0.9")
    # 다른 클라이언트는 영향 없음.
    with pytest.raises(AuthError):
        auth.login("ghostX", "whatever", client_key="10.0.0.99")
    auth.close()


def test_login_account_bucket_still_applies(tmp_path):
    # 계정 버킷: 같은 사용자명 반복 시도는 클라이언트가 달라도 계정 버킷으로 차단.
    auth = _auth(tmp_path, login_max_per_account=2, login_max_per_client=100)
    for i in range(2):
        with pytest.raises(AuthError):
            auth.login("victim", "nope", client_key=f"10.0.0.{i}")
    with pytest.raises(RateLimited):
        auth.login("victim", "nope", client_key="10.0.0.250")
    auth.close()


def test_signup_client_rate_limited(tmp_path):
    auth = _auth(tmp_path, signup_max_per_client=2)
    auth.signup("aaa", "password123", client_key="1.2.3.4")
    auth.signup("bbb", "password123", client_key="1.2.3.4")
    with pytest.raises(RateLimited):
        auth.signup("ccc", "password123", client_key="1.2.3.4")
    # 다른 클라이언트는 통과.
    auth.signup("ddd", "password123", client_key="5.6.7.8")
    auth.close()


def test_signup_without_client_key_not_limited(tmp_path):
    # 오프라인 도구(client_key 미지정)는 제한하지 않는다(기존 seed/import 경로 무영향).
    auth = _auth(tmp_path, signup_max_per_client=1)
    auth.signup("user1", "password123")
    auth.signup("user2", "password123")  # 제한 없음
    auth.close()


def test_inactive_account_still_verifies_password(tmp_path, monkeypatch):
    import hub.auth.passwords as pw

    auth = _auth(tmp_path)
    auth.signup("alice", "password123")
    user = auth.repo.get_user_by_username("alice")
    auth.repo.set_user_status(user["id"], "disabled", _now())

    calls: list[int] = []
    real = pw.verify_password
    monkeypatch.setattr(pw, "verify_password", lambda h, p: calls.append(1) or real(h, p))

    with pytest.raises(AuthError):
        auth.login("alice", "password123", client_key="1.1.1.1")
    # 비활성 계정도 Argon2 검증을 1회 수행(즉시 반환하면 타이밍으로 열거 가능 — 회귀 방지).
    assert len(calls) == 1
    auth.close()
