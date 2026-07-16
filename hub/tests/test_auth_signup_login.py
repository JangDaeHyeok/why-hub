"""인증 — 회원가입·로그인·세션·CSRF (구현스펙-인증인가-RBAC.md §3,§4).

웹 UI(build_web_app + auth) 를 TestClient 로 구동해 end-to-end 확인.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from hub.config import ApprovalConfig, Config
from hub.interfaces.web import build_web_app
from hub.service import KnowledgeService
from hub.tests.authhelpers import make_auth_service


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.approval = ApprovalConfig(enabled=True)
    svc = KnowledgeService(tmp_path, cfg)
    auth = make_auth_service(tmp_path)
    app = build_web_app(svc, auth)
    yield svc, auth, app
    svc.close()
    auth.close()


def _client(app):
    return TestClient(app, follow_redirects=False)


def test_public_signup_success(env):
    _, auth, app = env
    c = _client(app)
    r = c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    assert r.status_code == 303
    assert "wh_session" in r.headers.get("set-cookie", "")
    # 가입 즉시 active member.
    user = auth.repo.get_user_by_username("alice")
    assert user is not None and user["status"] == "active" and user["is_admin"] is False


def test_duplicate_username_rejected(env):
    _, _auth, app = env
    c = _client(app)
    c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    r = c.post("/ui/signup", data={"username": "Alice", "password": "supersecret123"})
    assert r.status_code == 400  # 정규화 후 중복


def test_password_hashed_not_plaintext(env):
    _, auth, app = env
    _client(app).post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    user = auth.repo.get_user_by_username("alice")
    assert user["password_hash"].startswith("$argon2id$")
    assert "supersecret123" not in user["password_hash"]
    # 저장소 어디에도 평문이 없다.
    dump = __import__("pathlib").Path(auth.repo.path).read_bytes()
    assert b"supersecret123" not in dump


def test_login_success_and_failure(env):
    _, _auth, app = env
    c = _client(app)
    c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    c.cookies.clear()
    ok = c.post("/ui/login", data={"username": "alice", "password": "supersecret123", "next": "/"})
    assert ok.status_code == 303 and "wh_session" in ok.headers.get("set-cookie", "")
    bad = c.post("/ui/login", data={"username": "alice", "password": "nope", "next": "/"})
    assert bad.status_code == 401


def test_protected_page_requires_login(env):
    _, _auth, app = env
    c = _client(app)
    r = c.get("/")
    assert r.status_code == 303 and "/ui/login" in r.headers["location"]


def test_logout_blocks_access(env):
    _, _auth, app = env
    c = _client(app)
    c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    assert c.get("/").status_code == 200
    # 로그아웃엔 CSRF 필요 — 세션 csrf 확보.
    import re
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/ui/account").text).group(1)
    c.post("/ui/logout", data={"csrf_token": csrf})
    c.cookies.clear()
    assert c.get("/").status_code == 303  # 재로그인 필요


def test_disabled_user_cannot_login(env):
    _, auth, app = env
    c = _client(app)
    c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    user = auth.repo.get_user_by_username("alice")
    auth.repo.set_user_status(user["id"], "disabled",
                              datetime.datetime.now().isoformat(timespec="seconds"))
    c.cookies.clear()
    r = c.post("/ui/login", data={"username": "alice", "password": "supersecret123", "next": "/"})
    assert r.status_code == 401


def test_session_cookie_attributes(env):
    _, _auth, app = env
    c = _client(app)
    r = c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    sc = r.headers.get("set-cookie", "").lower()
    assert "httponly" in sc and "samesite=lax" in sc and "path=/" in sc
    assert "secure" not in sc  # 로컬(cookie_secure=False)


def test_login_cross_origin_blocked(env):
    # 세션-전 login POST 는 Origin 이 요청 host 와 다르면 거부(login-CSRF 방어).
    _, _auth, app = env
    c = _client(app)
    c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    c.cookies.clear()
    r = c.post("/ui/login",
               data={"username": "alice", "password": "supersecret123", "next": "/"},
               headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    # 동일 출처(Origin=host)는 통과.
    ok = c.post("/ui/login",
                data={"username": "alice", "password": "supersecret123", "next": "/"},
                headers={"origin": "http://testserver"})
    assert ok.status_code == 303


def test_signup_cross_origin_blocked(env):
    _, _auth, app = env
    c = _client(app)
    r = c.post("/ui/signup", data={"username": "bob", "password": "supersecret123"},
               headers={"referer": "http://evil.example/x"})
    assert r.status_code == 403


def test_csrf_missing_and_mismatch_blocked(env):
    _, _auth, app = env
    c = _client(app)
    c.post("/ui/signup", data={"username": "alice", "password": "supersecret123"})
    # CSRF 누락 → 403
    r = c.post("/ui/account/password",
               data={"old_password": "supersecret123", "new_password": "newsecret9999"})
    assert r.status_code == 403
    # CSRF 불일치 → 403
    r = c.post("/ui/account/password",
               data={"old_password": "supersecret123", "new_password": "newsecret9999",
                     "csrf_token": "wrong"})
    assert r.status_code == 403
