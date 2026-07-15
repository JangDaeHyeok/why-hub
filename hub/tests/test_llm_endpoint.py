"""LLM 엔드포인트 클라이언트의 순수 변환/파싱 단위 테스트 (네트워크 없음).

메시지 변환(OpenAI 와이어 → Anthropic), 펑션콜 shim JSON 파싱, SSE delta 파서를 검증한다.
실통신(httpx)은 다루지 않는다 — 소비처 회귀는 주입형 Fake 로 커버(test_chat_functioncall 등).
"""

from __future__ import annotations

from hub.llm import (
    _iter_sse_deltas,
    _parse_tool_calls,
    _to_anthropic,
)


# ── _to_anthropic ────────────────────────────────────────────────────────


def test_system_extracted_and_user_blocked():
    system, msgs = _to_anthropic([
        {"role": "system", "content": "너는 도우미다."},
        {"role": "user", "content": "안녕"},
    ])
    assert system == "너는 도우미다."
    assert msgs == [{"role": "user", "content": [{"type": "text", "text": "안녕"}]}]


def test_assistant_tool_calls_rendered_as_json_text():
    _, msgs = _to_anthropic([
        {"role": "user", "content": "OAuth 문서 찾아줘"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "search_knowledge", "arguments": '{"query": "OAuth"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"hits": []}'},
    ])
    # user, assistant(tool_calls JSON), user(도구 결과)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assistant_text = msgs[1]["content"][0]["text"]
    assert '"tool_calls"' in assistant_text and "search_knowledge" in assistant_text
    assert "도구 결과(c1)" in msgs[2]["content"][0]["text"]


def test_consecutive_same_role_merged():
    # 한 assistant 턴이 2개 tool_calls → 2개 연속 tool 결과 → user 하나로 병합.
    _, msgs = _to_anthropic([
        {"role": "user", "content": "작업해줘"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function",
             "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    merged = msgs[2]["content"][0]["text"]
    assert "도구 결과(c1)" in merged and "도구 결과(c2)" in merged


def test_multiple_system_messages_joined():
    system, _ = _to_anthropic([
        {"role": "system", "content": "규칙1"},
        {"role": "user", "content": "x"},
        {"role": "system", "content": "규칙2"},
    ])
    assert "규칙1" in system and "규칙2" in system


# ── _parse_tool_calls ────────────────────────────────────────────────────


def test_parse_plain_json_tool_call():
    out = _parse_tool_calls('{"tool_calls":[{"name":"search_knowledge","arguments":{"query":"OAuth"}}]}')
    assert out["content"] is None
    assert out["tool_calls"] == [
        {"id": "call_1", "name": "search_knowledge", "arguments": {"query": "OAuth"}}
    ]


def test_parse_code_fenced_json():
    text = '```json\n{"tool_calls":[{"name":"get_document","arguments":{"id":"adr-0001"}}]}\n```'
    out = _parse_tool_calls(text)
    assert out["tool_calls"][0]["name"] == "get_document"
    assert out["tool_calls"][0]["arguments"] == {"id": "adr-0001"}


def test_parse_toolcall_embedded_in_prose():
    text = '네, 먼저 검색하겠습니다.\n{"tool_calls":[{"name":"search_knowledge","arguments":{"query":"x"}}]}'
    out = _parse_tool_calls(text)
    assert out["tool_calls"][0]["name"] == "search_knowledge"


def test_parse_plain_text_no_tools():
    out = _parse_tool_calls("미국의 수도는 워싱턴 D.C. 입니다.")
    assert out["tool_calls"] == []
    assert out["content"] == "미국의 수도는 워싱턴 D.C. 입니다."


def test_parse_missing_arguments_defaults_empty():
    out = _parse_tool_calls('{"tool_calls":[{"name":"list_documents"}]}')
    assert out["tool_calls"] == [{"id": "call_1", "name": "list_documents", "arguments": {}}]


# ── _iter_sse_deltas ─────────────────────────────────────────────────────


def test_sse_yields_deltas_until_done():
    lines = [
        'data: {"type":"meta","requestId":"x"}',
        "",
        'data: {"type":"delta","delta":"미"}',
        'data: {"type":"delta","delta":"국"}',
        'data: {"type":"done","text":"미국"}',
        'data: {"type":"delta","delta":"무시됨"}',  # done 이후는 나오지 않아야
    ]
    assert list(_iter_sse_deltas(lines)) == ["미", "국"]


def test_sse_ignores_malformed_and_empty():
    lines = ["", "not-json", 'data: {"type":"delta","delta":"ok"}', "data: {bad json}"]
    assert list(_iter_sse_deltas(lines)) == ["ok"]
