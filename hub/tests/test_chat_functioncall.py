"""멀티턴 AI 생성(펑션콜) 검증 (구현스펙-멀티턴생성-펑션콜.md).

가짜 LLM(tool_calls 방출)으로 도구 루프를 검증한다 — 실제 API 없이 결정론적.
- 읽기 도구 실시간 실행, 제안 도구는 staged(반영 안 됨)
- apply → 승인 큐 제출(pending), 관리자 승인 후 반영
- 최종 응답 스트리밍(chat_stream) 이벤트: session/tool/token/done
- lint_check 자가 교정, propose_deprecate 는 폐기 상태로 스테이징
"""

from __future__ import annotations

import pytest

from hub.config import ApprovalConfig, Config
from hub.tests.authhelpers import admin
from hub.service import KnowledgeService


def _adr(id="adr-0001", status="accepted", title="인증 방식", alt="JWT 는 폐기 지연으로 기각."):
    return (
        f"---\nid: {id}\ntype: adr\ntitle: {title}\nstatus: {status}\n"
        "created: 2026-06-01\ntags: [auth]\n---\n\n"
        "# 배경\n\n맥락.\n\n# 결정\n\n서버 세션.\n\n# 근거\n\n즉시 폐기.\n\n"
        f"# 대안\n\n{alt}\n\n# 결과\n\n서버 의존.\n"
    )


def _cfg():
    c = Config()
    c.approval = ApprovalConfig(enabled=True)
    return c


class ScriptedLLM:
    """정해진 순서로 tool_calls / content 를 방출하는 가짜 LLM. calls 로그로 도구 실행 추적."""

    available = True

    def __init__(self, script):
        self._script = list(script)
        self.i = 0
        self.seen_tools: list[str] = []

    def chat(self, messages, tools=None):
        # tool 결과 메시지를 관찰(직전에 실행된 도구 추적).
        for m in messages:
            if m.get("role") == "tool" and m["tool_call_id"] not in self.seen_tools:
                self.seen_tools.append(m["tool_call_id"])
        step = self._script[min(self.i, len(self._script) - 1)]
        self.i += 1
        return step

    def chat_stream(self, messages, tools=None):
        for t in ["초안", "을 ", "제안", "합니다."]:
            yield t


@pytest.fixture()
def svc(tmp_path):
    s = KnowledgeService(tmp_path, _cfg())
    yield s
    s.close()


def _seed(svc, md):
    """승인 게이트를 우회해 문서 하나 반영(테스트 준비용)."""
    svc.config.approval.enabled = False
    svc.save_document(md, actor="seed")
    svc.config.approval.enabled = True


def test_tool_loop_reads_then_stages(svc):
    _seed(svc, _adr())
    new_md = _adr(id="adr-0002", status="proposed", title="OAuth 도입")
    llm = ScriptedLLM([
        {"content": None, "tool_calls": [{"id": "c1", "name": "search_knowledge",
                                          "arguments": {"query": "인증"}}]},
        {"content": None, "tool_calls": [{"id": "c2", "name": "get_document",
                                          "arguments": {"id": "adr-0001"}}]},
        {"content": None, "tool_calls": [{"id": "c3", "name": "propose_create",
                                          "arguments": {"target_type": "adr", "markdown": new_md}}]},
        {"content": "제안했습니다. 적용할까요?", "tool_calls": []},
    ])
    r = svc.chat_turn(None, "OAuth ADR 만들어줘", actor="carol", llm=llm)
    # 읽기 도구 2개 실행됨(tool 결과가 대화에 반영).
    assert len(llm.seen_tools) >= 2
    # 제안은 staged 로만(반영 안 됨).
    assert [s["op"] for s in r["staged"]] == ["create"]
    assert svc.get_document("adr-0002") is None

    # 적용 → 승인 큐 제출(pending).
    subs = svc.apply_session(r["session_id"], actor="carol")
    assert subs[0]["doc_id"] == "adr-0002" and subs[0]["status"] == "pending"
    assert svc.get_document("adr-0002") is None  # 아직 미반영
    # 관리자 승인 후 반영.
    svc.approve_submission(subs[0]["submission_id"], principal=admin("alice"))
    assert svc.get_document("adr-0002") is not None
    # staged 는 apply 후 비워짐.
    assert svc.get_session(r["session_id"])["staged"] == []


def test_stream_events_order(svc):
    llm = ScriptedLLM([
        {"content": None, "tool_calls": [{"id": "c1", "name": "list_documents",
                                          "arguments": {}}]},
        {"content": "정리됨", "tool_calls": []},
    ])
    events = list(svc.chat_turn_stream(None, "안녕", actor="carol", llm=llm))
    types = [e["type"] for e in events]
    assert types[0] == "session"
    assert "tool" in types and "token" in types
    assert types[-1] == "done"
    # 토큰을 이어붙이면 최종 reply.
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    assert events[-1]["reply"] == tokens
    assert events[-1]["staged"] == []


def test_lint_check_reports_missing_sections(svc):
    # lint_check 도구가 대안 누락을 잡아낸다(자가 교정 근거).
    bad = _adr(id="adr-0003", alt="")
    llm = ScriptedLLM([
        {"content": None, "tool_calls": [{"id": "c1", "name": "lint_check",
                                          "arguments": {"markdown": bad}}]},
        {"content": "대안 섹션을 채워야 합니다.", "tool_calls": []},
    ])
    r = svc.chat_turn(None, "이 초안 검토해줘", actor="carol", llm=llm)
    assert r["staged"] == []  # 제안 안 함
    assert llm.seen_tools  # lint_check 실행됨


def test_propose_deprecate_stages_deprecated_status(svc):
    _seed(svc, _adr())
    llm = ScriptedLLM([
        {"content": None, "tool_calls": [{"id": "c1", "name": "propose_deprecate",
                                          "arguments": {"doc_id": "adr-0001",
                                                        "reason": "OAuth 로 대체"}}]},
        {"content": "폐기를 제안했습니다.", "tool_calls": []},
    ])
    r = svc.chat_turn(None, "adr-0001 폐기해줘", actor="carol", llm=llm)
    assert [s["op"] for s in r["staged"]] == ["deprecate"]
    subs = svc.apply_session(r["session_id"], actor="carol")
    svc.approve_submission(subs[0]["submission_id"], principal=admin("alice"))
    assert svc.get_document("adr-0001")["status"] == "deprecated"


def test_apply_is_retry_safe(svc):
    # 여러 staged 중 뒤 항목이 실패하면, 이미 제출된 앞 항목은 staged 에서 제거되어
    # 재시도가 중복 제출하지 않는다(P2).
    orch = svc._orchestrator()
    sid = orch.new_session(actor="carol", project="why-hub")
    sess = orch.get(sid)
    sess["staged"] = [
        {"op": "create", "markdown": _adr("adr-0001"), "doc_id": None,
         "project": None, "prelint": {}},
        {"op": "create", "markdown": _adr("adr-0002"), "doc_id": None,
         "project": None, "prelint": {}},
    ]
    orig = svc.submit_change

    def flaky(md, **kw):
        if "adr-0002" in md:
            raise RuntimeError("boom")
        return orig(md, **kw)

    svc.submit_change = flaky
    with pytest.raises(RuntimeError):
        orch.apply(sid, actor="carol")
    assert len(sess["staged"]) == 1  # 첫 항목만 제거, 실패 항목 잔존

    svc.submit_change = orig  # 복구 후 재시도
    subs2 = orch.apply(sid, actor="carol")
    assert len(subs2) == 1 and sess["staged"] == []
    # adr-0001 은 정확히 한 번만 제출됨(중복 없음).
    pend = [s["doc_id"] for s in svc.list_submissions("pending")]
    assert pend.count("adr-0001") == 1


def test_session_pinned_to_owner(svc):
    # 세션은 생성 사용자에 고정 — 다른 사용자가 session_id 를 넘겨 탈취할 수 없다(코드리뷰 5).
    from hub.auth.principal import Principal

    carol = Principal.for_user("carol")
    dave = Principal.for_user("dave")
    llm = ScriptedLLM([{"content": "안녕하세요", "tool_calls": []}])
    r = svc.chat_turn(None, "시작", actor="carol", llm=llm, principal=carol)
    sid = r["session_id"]
    # 다른 사용자가 같은 세션으로 턴 시도 → 거부(principal 교체·staged 탈취 차단).
    with pytest.raises(PermissionError):
        svc.chat_turn(sid, "탈취", actor="dave", llm=llm, principal=dave)
    # 다른 사용자가 apply 시도 → 거부.
    with pytest.raises(PermissionError):
        svc.apply_session(sid, actor="dave", principal=dave)
    # 소유자는 계속 접근 가능.
    r2 = svc.chat_turn(sid, "계속", actor="carol", llm=llm, principal=carol)
    assert r2["session_id"] == sid


def test_chat_requires_llm(tmp_path):
    from hub.llm import LLMUnavailable

    s = KnowledgeService(tmp_path, _cfg())  # LLM 미구성
    with pytest.raises(LLMUnavailable):
        s.chat_turn(None, "안녕", actor="carol")
    s.close()
