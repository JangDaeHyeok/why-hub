"""인증 — JWT 검증 매트릭스 · MCP streamable-http 인가 · stdio 거부 (구현스펙-인증인가-RBAC.md §6).

JWT 검증 매트릭스는 JWTVerifier.verify_token 을 직접 검사한다(검증 실패 = 서버 401). member/admin
도구 인가와 무토큰 거부는 실제 streamable-http 서버를 스레드로 띄워 확인한다.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import jwt
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier

from hub.auth.jwt_service import JwtIssuer, generate_rsa_keypair
from hub.config import ApprovalConfig, Config
from hub.interfaces.mcp_server import build_mcp
from hub.service import KnowledgeService
from hub.tests.authhelpers import keypair

ISS, AUD = "why-hub", "why-hub-mcp"


def _issuer():
    priv, pub = keypair()
    return JwtIssuer(private_key_pem=priv, public_key_pem=pub, issuer=ISS, audience=AUD)


def _verifier():
    _, pub = keypair()
    return JWTVerifier(public_key=pub, issuer=ISS, audience=AUD, algorithm="RS256")


def _verify(token: str):
    return asyncio.run(_verifier().verify_token(token))


# ── JWT 검증 매트릭스 (실패 = 서버 401) ─────────────────────────────────
def test_valid_token_accepted():
    tok, _ = _issuer().issue(subject="u1", username="carol", is_admin=False,
                             scopes=["knowledge:read"])
    acc = _verify(tok)
    assert acc is not None and "knowledge:read" in acc.scopes


def test_expired_token_rejected():
    iss = _issuer()
    tok, _ = iss.issue(subject="u1", username="c", is_admin=False, scopes=["knowledge:read"],
                       now=int(time.time()) - 7200)  # exp = now-7200+600 → 과거
    assert _verify(tok) is None


def test_bad_signature_rejected():
    other_priv, _ = generate_rsa_keypair()
    _, pub = keypair()
    forged = JwtIssuer(private_key_pem=other_priv, public_key_pem=pub, issuer=ISS, audience=AUD)
    tok, _ = forged.issue(subject="u1", username="c", is_admin=False, scopes=["knowledge:read"])
    assert _verify(tok) is None  # 서명이 실제 키와 불일치


def test_bad_issuer_rejected():
    priv, pub = keypair()
    bad = JwtIssuer(private_key_pem=priv, public_key_pem=pub, issuer="evil", audience=AUD)
    tok, _ = bad.issue(subject="u1", username="c", is_admin=False, scopes=["knowledge:read"])
    assert _verify(tok) is None


def test_bad_audience_rejected():
    priv, pub = keypair()
    bad = JwtIssuer(private_key_pem=priv, public_key_pem=pub, issuer=ISS, audience="other-aud")
    tok, _ = bad.issue(subject="u1", username="c", is_admin=False, scopes=["knowledge:read"])
    assert _verify(tok) is None


def test_disallowed_algorithm_rejected():
    # HS256 공유 secret 토큰은 RS256 검증기에서 거부돼야 한다(alg 혼동 방지).
    hs = jwt.encode({"iss": ISS, "aud": AUD, "sub": "u1", "scope": "knowledge:read"},
                    "shared-secret", algorithm="HS256")
    assert _verify(hs) is None


# ── 실제 streamable-http 서버 ───────────────────────────────────────────
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def mcp_url(tmp_path):
    cfg = Config()
    cfg.approval = ApprovalConfig(enabled=True)
    svc = KnowledgeService(tmp_path, cfg)
    mcp = build_mcp(svc, _verifier())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mcp.http_app(), host="127.0.0.1", port=port,
                                           log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):  # 기동 대기
        if server.started:
            break
        time.sleep(0.05)
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/mcp/"
    server.should_exit = True
    th.join(timeout=5)
    svc.close()


_ADR = ("---\nid: adr-0001\ntype: adr\ntitle: t\nstatus: accepted\ncreated: 2026-06-01\n---\n\n"
        "# 배경\nx\n# 결정\ny\n# 근거\nz\n# 대안\na\n# 결과\nb\n")


def _call(url, token, name, args):
    async def go():
        async with Client(url, auth=token) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go()).data


def test_no_token_rejected(mcp_url):
    async def go():
        async with Client(mcp_url) as c:
            await c.list_tools()
    with pytest.raises(Exception):  # noqa: B017 - HTTP 401
        asyncio.run(go())


def test_member_can_read_admin_cannot_be_bypassed(mcp_url):
    iss = _issuer()
    member, _ = iss.issue(subject="u1", username="carol", is_admin=False,
                          scopes=["knowledge:read", "knowledge:submit"])
    admin, _ = iss.issue(subject="u2", username="alice", is_admin=True,
                         scopes=["knowledge:read", "knowledge:submit", "knowledge:review"])
    # member 저장(submit) → pending
    r = _call(mcp_url, member, "save_document", {"markdown": _ADR})
    sub_id = r["submission_id"]
    assert r["status"] == "pending"
    # member 승인 시도 → 거부(review scope 없음)
    with pytest.raises(Exception):  # noqa: B017 - ToolError
        _call(mcp_url, member, "approve_submission", {"sub_id": sub_id})
    # admin 승인 → 성공, 이력 actor 는 원제출자(carol)
    res = _call(mcp_url, admin, "approve_submission", {"sub_id": sub_id})
    assert res["id"] == "adr-0001"
    hist = _call(mcp_url, admin, "get_history", {"id": "adr-0001"})
    assert hist[0]["actor"] == "carol"


# ── 인증 활성 + stdio → 기동 거부 ───────────────────────────────────────
def test_stdio_with_auth_refused(monkeypatch, tmp_path):
    from hub.interfaces import mcp_server

    priv, pub = keypair()
    key = tmp_path / "pub.pem"
    key.write_text(pub, encoding="utf-8")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_PUBLIC_KEY_FILE", str(key))
    monkeypatch.setenv("KNOWLEDGE_HUB_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("KNOWLEDGE_HUB_ROOT", str(tmp_path))
    with pytest.raises(SystemExit):
        mcp_server.main()
