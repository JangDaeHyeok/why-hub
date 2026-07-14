"""P12 검증 — 인제스천 + curate ([Δ] §10, 기획안1 §8, CLAUDE.md §3).

- 같은 source 재입력 → 신규 아닌 갱신(멱등)
- curate on/off 동작, LLM 없을 때 skip
- service + MCP + HTTP(/ingest) 노출
"""

from __future__ import annotations

import asyncio

import pytest

from fastapi.testclient import TestClient
from fastmcp import Client

from hub.interfaces.http_api import build_app
from hub.interfaces.mcp_server import build_mcp
from hub.service import KnowledgeService


@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path)
    yield s
    s.close()


# ── 인제스천 (멱등) ───────────────────────────────────────────────────
def test_ingest_creates_new_reference(svc):
    res = svc.ingest_source(
        "notion:page-1", content="# 회의록\n\n캐시는 Redis 로 간다.\n",
        title="회의록", now="2026-07-14T10:00:00",
    )
    assert res.change_type == "ingest"
    doc = svc.get_document(res.id)
    assert doc["type"] == "reference"
    assert "Redis" in doc["body"]
    # 프로버넌스: 이력이 ingest 타입으로 기록
    assert svc.get_history(res.id)[0]["type"] == "ingest"


def test_ingest_same_source_updates_not_duplicates(svc):
    r1 = svc.ingest_source("notion:page-1", content="# A\n\n첫 버전.\n",
                           now="2026-07-14T10:00:00")
    r2 = svc.ingest_source("notion:page-1", content="# A\n\n둘째 버전 갱신.\n",
                           now="2026-07-14T11:00:00")
    # 같은 source → 같은 id (신규 아님)
    assert r1.id == r2.id
    assert len(svc.list_documents()) == 1
    # 내용 갱신 반영
    assert "둘째 버전" in svc.get_document(r1.id)["body"]
    # 이력: ingest(신규) + ingest(갱신)
    assert [e["type"] for e in svc.get_history(r1.id)] == ["ingest", "ingest"]


def test_ingest_idempotent_noop_on_identical(svc):
    svc.ingest_source("notion:page-1", content="# A\n\n같은 내용.\n",
                      now="2026-07-14T10:00:00")
    svc.ingest_source("notion:page-1", content="# A\n\n같은 내용.\n",
                      now="2026-07-14T11:00:00")
    # 동일 내용 재인제스트 → 변경 없음 → 이력 1개
    assert len(svc.get_history(next(d["id"] for d in svc.list_documents()))) == 1


# ── curate (LLM on/off) ───────────────────────────────────────────────
class _FakeLLM:
    def __init__(self, available=True, reply="압축 요약"):
        self._available = available
        self._reply = reply
        self.calls = []

    @property
    def available(self):
        return self._available

    def complete(self, prompt, *, system=None):
        self.calls.append(prompt)
        return self._reply


def test_curate_skips_when_llm_unavailable(svc):
    svc.ingest_source("s1", content="# A\n\nRedis 캐시.\n", now="2026-07-14T10:00:00")
    ids = [d["id"] for d in svc.list_documents()]
    out = svc.curate("캐시 전략", ids)  # 기본 LLMClient 미구성 → skip
    assert out["skipped"] is True
    assert out["summary"] is None
    assert out["candidate_ids"] == ids


def test_curate_runs_when_llm_available(svc):
    svc.ingest_source("s1", content="# A\n\nRedis 캐시.\n", now="2026-07-14T10:00:00")
    ids = [d["id"] for d in svc.list_documents()]
    fake = _FakeLLM(available=True, reply="Redis 로 캐시한다.")
    out = svc.curate("캐시 전략", ids, llm=fake)
    assert out["skipped"] is False
    assert out["summary"] == "Redis 로 캐시한다."
    assert fake.calls and "캐시 전략" in fake.calls[0]


# ── 인터페이스 노출 ───────────────────────────────────────────────────
def test_ingest_via_http(tmp_path):
    s = KnowledgeService(tmp_path)
    with TestClient(build_app(s)) as c:
        r = c.post("/ingest", json={"source_ref": "sheet:1", "content": "# T\n\n표 데이터.\n"})
        assert r.status_code == 200
        doc_id = r.json()["id"]
        # 재입력 → 같은 id (멱등)
        r2 = c.post("/ingest", json={"source_ref": "sheet:1", "content": "# T\n\n갱신.\n"})
        assert r2.json()["id"] == doc_id
        assert len(c.get("/docs").json()) == 1
    s.close()


def test_ingest_and_curate_via_mcp(tmp_path):
    s = KnowledgeService(tmp_path)
    mcp = build_mcp(s)

    async def go():
        async with Client(mcp) as c:
            ing = (await c.call_tool(
                "ingest_source", {"source_ref": "n:1", "content": "# A\n\nRedis.\n"}
            )).data
            cur = (await c.call_tool(
                "curate", {"query": "q", "candidate_ids": [ing["id"]]}
            )).data
            return ing, cur

    ing, cur = asyncio.run(go())
    assert ing["change_type"] == "ingest"
    assert cur["skipped"] is True  # 테스트 환경 LLM 미구성
    s.close()
