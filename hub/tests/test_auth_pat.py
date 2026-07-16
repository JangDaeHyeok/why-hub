"""인증 — PAT 생성·폐기·JWT 교환 (구현스펙-인증인가-RBAC.md §5,§6)."""

from __future__ import annotations

import datetime
import pathlib

import pytest
from fastapi.testclient import TestClient

from hub.auth.principal import SCOPE_READ, SCOPE_REVIEW, SCOPE_SUBMIT
from hub.auth.service import AuthError
from hub.interfaces.http_api import build_app
from hub.service import KnowledgeService
from hub.tests.authhelpers import make_auth_service, make_user


@pytest.fixture()
def env(tmp_path):
    svc = KnowledgeService(tmp_path)
    auth = make_auth_service(tmp_path)
    yield svc, auth
    svc.close()
    auth.close()


def test_pat_secret_returned_once_and_not_stored(env):
    _, auth = env
    user = make_user(auth, "alice")
    full, view = auth.create_pat(user, name="ci", scopes=[SCOPE_SUBMIT])
    assert full.startswith("whp_")
    assert "secret_hash" not in view  # 목록/뷰엔 secret 없음
    # DB에 원문 미저장 — 파일 어디에도 full/secret 이 없다.
    dump = pathlib.Path(auth.repo.path).read_bytes()
    assert full.encode() not in dump
    secret = full.rsplit("_", 1)[-1]
    assert secret.encode() not in dump


def test_only_own_pats_listed_and_revoked(env):
    _, auth = env
    alice = make_user(auth, "alice")
    bob = make_user(auth, "bob")
    _, pa = auth.create_pat(alice, name="a", scopes=[SCOPE_READ])
    assert [p["id"] for p in auth.list_pats(alice["id"])] == [pa["id"]]
    assert auth.list_pats(bob["id"]) == []
    # 타인 PAT 폐기 시도 실패(자신의 것만).
    assert auth.revoke_pat(pa["id"], bob["id"]) is False
    assert auth.revoke_pat(pa["id"], alice["id"]) is True


def test_scope_escalation_blocked(env):
    _, auth = env
    member = make_user(auth, "carol")  # member — review 없음
    with pytest.raises(PermissionError):
        auth.create_pat(member, name="x", scopes=[SCOPE_REVIEW])


def test_expired_pat_exchange_fails(env):
    _, auth = env
    user = make_user(auth, "alice")
    past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
    full, _ = auth.create_pat(user, name="old", scopes=[SCOPE_READ], expires_at=past)
    with pytest.raises(AuthError):
        auth.exchange_pat_for_jwt(full)


def test_revoked_pat_exchange_fails(env):
    _, auth = env
    user = make_user(auth, "alice")
    full, pat = auth.create_pat(user, name="r", scopes=[SCOPE_READ])
    auth.revoke_pat(pat["id"], user["id"])
    with pytest.raises(AuthError):
        auth.exchange_pat_for_jwt(full)


def test_valid_exchange_returns_jwt(env):
    _, auth = env
    user = make_user(auth, "alice")
    full, _ = auth.create_pat(user, name="ok", scopes=[SCOPE_READ, SCOPE_SUBMIT])
    res = auth.exchange_pat_for_jwt(full)
    assert res["token_type"] == "Bearer" and res["expires_in"] == 600
    assert set(res["scope"].split()) == {SCOPE_READ, SCOPE_SUBMIT}
    import jwt as _jwt
    claims = _jwt.decode(res["access_token"], auth.issuer.public_key_pem,
                         algorithms=["RS256"], audience="why-hub-mcp", issuer="why-hub")
    assert claims["sub"] == user["id"] and claims["username"] == "alice"


def test_http_exchange_endpoint_no_store(env):
    svc, auth = env
    user = make_user(auth, "alice")
    full, _ = auth.create_pat(user, name="ci", scopes=[SCOPE_READ])
    c = TestClient(build_app(svc, auth))
    r = c.post("/api/auth/token/exchange", headers={"Authorization": f"Bearer {full}"})
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    assert r.json()["token_type"] == "Bearer"
    # 잘못된 PAT → 401
    assert c.post("/api/auth/token/exchange",
                  headers={"Authorization": "Bearer whp_bad_x"}).status_code == 401
