"""P11 검증 — 계보(get_related).

- supersedes 체인 추적(A→B→C, 정방향/역방향)
- related 양방향
- 순환 입력에도 종료
- service + MCP + HTTP 노출
"""

from __future__ import annotations

import asyncio

import pytest

from fastapi.testclient import TestClient
from fastmcp import Client

from hub.interfaces.http_api import build_app
from hub.interfaces.mcp_server import build_mcp
from hub.service import KnowledgeService


def _doc(id, *, supersedes=None, related=None, status="accepted"):
    fm = [f"id: {id}", "type: adr", f"title: {id}", f"status: {status}",
          "created: 2026-06-01"]
    if supersedes:
        fm.append(f"supersedes: {supersedes}")
    if related:
        fm.append("related: [" + ", ".join(related) + "]")
    front = "\n".join(fm)
    return (
        f"---\n{front}\n---\n\n"
        "# 배경\n\nx\n\n# 결정\n\ny\n\n# 근거\n\nz\n\n"
        "# 대안\n\n다른 안은 근거 부족으로 기각.\n\n# 결과\n\nw\n"
    )


@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path)
    yield s
    s.close()


def test_supersedes_chain_forward_and_backward(svc):
    # C(존재) ← B supersedes C ← A supersedes B  (순서대로 저장: 대상이 먼저 존재해야 dangling 없음)
    svc.save_document(_doc("adr-0003"), actor="a", now="2026-07-14T10:00:00")
    svc.save_document(_doc("adr-0002", supersedes="adr-0003"), actor="a", now="2026-07-14T10:01:00")
    svc.save_document(_doc("adr-0001", supersedes="adr-0002"), actor="a", now="2026-07-14T10:02:00")

    top = svc.get_related("adr-0001")
    assert top["supersedes"] == ["adr-0002", "adr-0003"]  # 정방향 체인
    assert top["superseded_by"] == []

    bottom = svc.get_related("adr-0003")
    assert bottom["superseded_by"] == ["adr-0002", "adr-0001"]  # 역방향 체인
    assert bottom["supersedes"] == []


def test_related_bidirectional(svc):
    svc.save_document(_doc("adr-0001"), actor="a", now="2026-07-14T10:00:00")
    svc.save_document(_doc("adr-0002", related=["adr-0001"]), actor="a", now="2026-07-14T10:01:00")

    # adr-0002 → adr-0001 (outgoing)
    assert svc.get_related("adr-0002")["related"] == ["adr-0001"]
    # adr-0001 은 adr-0002 를 명시 안 했지만 양방향으로 잡힌다 (incoming)
    assert svc.get_related("adr-0001")["related"] == ["adr-0002"]


def test_cycle_terminates(svc):
    # A supersedes B, B supersedes A (순환) — 종료해야 한다.
    svc.save_document(_doc("adr-0002"), actor="a", now="2026-07-14T10:00:00")
    svc.save_document(_doc("adr-0001", supersedes="adr-0002"), actor="a", now="2026-07-14T10:01:00")
    # adr-0002 를 갱신해 adr-0001 을 supersede → 순환 형성
    svc.save_document(_doc("adr-0002", supersedes="adr-0001"), actor="a", now="2026-07-14T10:02:00")

    r = svc.get_related("adr-0001")  # 무한루프 없이 종료
    assert set(r["supersedes"]) == {"adr-0002"}  # 자기 자신은 포함 안 함


def test_get_related_missing_returns_none(svc):
    assert svc.get_related("nope-0001") is None


def test_related_via_mcp_and_http(tmp_path):
    svc = KnowledgeService(tmp_path)
    svc.save_document(_doc("adr-0002"), actor="a", now="2026-07-14T10:00:00")
    svc.save_document(_doc("adr-0001", supersedes="adr-0002"), actor="a", now="2026-07-14T10:01:00")

    # MCP
    async def go():
        async with Client(build_mcp(svc)) as c:
            return (await c.call_tool("get_related", {"id": "adr-0001"})).data

    assert asyncio.run(go())["supersedes"] == ["adr-0002"]

    # HTTP
    with TestClient(build_app(svc)) as c:
        r = c.get("/docs/adr-0001/related")
        assert r.status_code == 200
        assert r.json()["supersedes"] == ["adr-0002"]
        assert c.get("/docs/nope-0001/related").status_code == 404

    svc.close()
