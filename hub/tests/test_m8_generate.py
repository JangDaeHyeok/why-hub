"""M8 검증 — AI 생성 (`/generate` + 생성 UI).

- 초안 반환만(저장 안 함), 저장 전 lint 결과 동반
- 관련 기존 ADR 자동 수집(유사 RAG)
- LLM 미구성 시 비활성(503) — 직접 작성은 항상 가능
- UI: 생성 → 편집 화면(검토) → save 는 사람이
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from hub.interfaces.web import build_web_app
from hub.service import KnowledgeService

VALID_DRAFT = (
    "---\nid: adr-0009\ntype: adr\ntitle: 생성된 결정\nstatus: proposed\n"
    "created: 2026-07-14\n---\n\n"
    "# 배경\n\n맥락.\n\n# 결정\n\n무언가로 정한다.\n\n"
    "# 근거\n\n이유.\n\n# 대안\n\n다른 안은 근거 부족으로 기각.\n\n# 결과\n\n영향.\n"
)
DRAFT_MISSING_ALT = VALID_DRAFT.replace("다른 안은 근거 부족으로 기각.", "")


class FakeLLM:
    def __init__(self, reply, available=True):
        self.reply = reply
        self._available = available
        self.calls = []

    @property
    def available(self):
        return self._available

    def complete(self, prompt, *, system=None):
        self.calls.append((system, prompt))
        return self.reply


def _adr(id, title, decision):
    return (
        f"---\nid: {id}\ntype: adr\ntitle: {title}\nstatus: accepted\ncreated: 2026-06-01\n---\n\n"
        "# 배경\n\nx\n\n"
        f"# 결정\n\n{decision}\n\n# 근거\n\ny\n\n# 대안\n\n기각안.\n\n# 결과\n\nz\n"
    )


# ── 서비스 계약 ───────────────────────────────────────────────────────
def test_generate_returns_draft_without_saving(tmp_path):
    svc = KnowledgeService(tmp_path, llm=FakeLLM(VALID_DRAFT))
    out = svc.generate_draft("adr", [{"kind": "note", "text": "인증을 세션으로"}], "인증 ADR")
    assert out["draft_markdown"].startswith("---")
    assert out["lint"]["ok"] is True
    assert "note:inline" in out["used_sources"]
    # 저장 안 됨
    assert svc.list_documents() == []
    svc.close()


def test_generate_prelint_reports_missing_alternatives(tmp_path):
    svc = KnowledgeService(tmp_path, llm=FakeLLM(DRAFT_MISSING_ALT))
    out = svc.generate_draft("adr", [{"kind": "note", "text": "x"}], "h")
    assert out["lint"]["ok"] is False
    assert any("대안" in r for r in out["lint"]["reasons"])
    svc.close()


def test_generate_collects_related_adrs(tmp_path):
    fake = FakeLLM(VALID_DRAFT)
    svc = KnowledgeService(tmp_path, llm=fake)
    svc.save_document(_adr("adr-0001", "세션 인증", "세션 방식과 Redis 를 쓴다."),
                      actor="a", now="2026-07-14T10:00:00")
    out = svc.generate_draft("adr", [{"kind": "note", "text": "세션 인증 재검토"}], "세션")
    assert "adr-0001" in out["related_context"]
    # 관련 ADR 이 프롬프트 컨텍스트에 포함
    assert "adr-0001" in fake.calls[0][1]
    svc.close()


def test_generate_raises_when_llm_unavailable(tmp_path):
    from hub.llm import LLMUnavailable

    svc = KnowledgeService(tmp_path, llm=FakeLLM("x", available=False))
    with pytest.raises(LLMUnavailable):
        svc.generate_draft("adr", [], "h")
    svc.close()


# ── HTTP /generate ────────────────────────────────────────────────────
def test_http_generate_ok(tmp_path):
    svc = KnowledgeService(tmp_path, llm=FakeLLM(VALID_DRAFT))
    with TestClient(build_web_app(svc)) as c:
        r = c.post("/generate", json={"target_type": "adr",
                                      "sources": [{"kind": "note", "text": "x"}], "hint": "h"})
        assert r.status_code == 200
        assert r.json()["lint"]["ok"] is True
    svc.close()


def test_http_generate_503_when_llm_off(tmp_path):
    svc = KnowledgeService(tmp_path)  # LLM 미구성
    with TestClient(build_web_app(svc)) as c:
        r = c.post("/generate", json={"target_type": "adr", "sources": [], "hint": "h"})
        assert r.status_code == 503
    svc.close()


# ── 생성 UI (경로 B) ──────────────────────────────────────────────────
def test_ui_generate_form(tmp_path):
    svc = KnowledgeService(tmp_path, llm=FakeLLM(VALID_DRAFT))
    with TestClient(build_web_app(svc)) as c:
        r = c.get("/ui/generate")
        assert r.status_code == 200
        assert 'name="target_type"' in r.text
    svc.close()


def test_ui_generate_produces_editable_draft(tmp_path):
    svc = KnowledgeService(tmp_path, llm=FakeLLM(VALID_DRAFT))
    with TestClient(build_web_app(svc)) as c:
        r = c.post("/ui/generate", data={"target_type": "adr", "hint": "h",
                                         "source_ids": "", "source_text": "인증"})
        assert r.status_code == 200
        # 초안이 편집 폼에 실려 옴 (검토용), 저장은 아직 아님
        assert "AI 초안 검토" in r.text
        assert 'name="markdown"' in r.text
        assert "생성된 결정" in r.text
        assert svc.list_documents() == []  # 저장 안 됨
    svc.close()


def test_ui_generate_llm_off_shows_notice(tmp_path):
    svc = KnowledgeService(tmp_path)  # LLM 미구성
    with TestClient(build_web_app(svc)) as c:
        r = c.post("/ui/generate", data={"target_type": "adr", "hint": "",
                                         "source_ids": "", "source_text": "x"})
        assert r.status_code == 503
        assert "직접 작성" in r.text
    svc.close()
