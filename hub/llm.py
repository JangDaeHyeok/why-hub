"""LLM 클라이언트 — 커스텀 HTTP 엔드포인트 래퍼 (curate·요약·AI 생성·멀티턴 채팅).

운영 LLM 은 **Anthropic Messages 스타일**의 두 엔드포인트로 노출된다 (CLAUDE.md §3):
  - complete_url (논스트리밍): `{text, thinking, ...}` 를 한 번에 반환.
  - stream_url   (스트리밍): SSE — `data: {"type":"delta","delta":...}` … `data: {"type":"done",...}`.

요청 포맷: `{system:<str>, messages:[{role, content:[{type:"text", text}]}], max_tokens, effort}`.
`temperature`/`top_p`/`top_k` 는 보내지 않는다 (Sonnet 5 는 non-default 시 400 — 다양성은 system 으로 유도).

이 엔드포인트는 **네이티브 펑션콜을 지원하지 않는다**. 멀티턴 도구 루프(chat.py)의 계약
(`chat()` 이 `tool_calls` 를 반환)은 **프롬프트 shim** 으로 흡수한다: system 에 도구 스키마 +
"`{\"tool_calls\":[...]}` JSON 만 출력하라"는 지시를 심고, 응답 text 를 파싱해 tool_calls 로 합성한다.

미구성(엔드포인트 URL 없음) 시 `available` 이 False 이며, 소비자는 **graceful skip** 한다.

구현 Phase: P12 (백엔드 전환).
"""

from __future__ import annotations

import json

import httpx

from .config import LLMConfig

# 엔드포인트 호출 타임아웃 (초). 초안 생성/멀티턴은 지연이 길 수 있어 넉넉히 둔다.
_TIMEOUT = 120.0


class LLMUnavailable(RuntimeError):
    """LLM 미구성 — 해당 기능(예: /generate)은 비활성."""


class LLMClient:
    """커스텀 HTTP 엔드포인트(Anthropic 스타일) 클라이언트(얇은 래퍼)."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    @property
    def available(self) -> bool:
        """complete/stream 엔드포인트 URL 이 모두 설정됐을 때만 True (공개 URL — api key 불필요)."""
        return bool(self.cfg.complete_url and self.cfg.stream_url)

    # ── 단일 완성 (curate·요약·AI 초안 생성) ─────────────────────────────
    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """1회 완성. 미구성이면 RuntimeError — 호출 전 `available` 로 가드할 것."""
        self._require()
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        sys_str, amsgs = _to_anthropic(msgs)
        data = self._post(self.cfg.complete_url, sys_str, amsgs)
        return data.get("text") or ""

    # ── 멀티턴 (펑션콜 shim) ──────────────────────────────────────────────
    def chat(self, messages: list[dict], *, tools: list[dict] | None = None) -> dict:
        """멀티턴 1스텝 (펑션콜 단계). 미구성이면 RuntimeError.

        messages 는 OpenAI 와이어 포맷(system/user/assistant/tool). tools 전달 시 프롬프트 shim 으로
        도구 사용을 유도한다. 반환: {"content": str|None, "tool_calls": [{"id","name","arguments"(dict)}]}.
        arguments 는 파싱된 dict — 오케스트레이터가 재적재 시 다시 직렬화한다(계약 유지).
        """
        self._require()
        sys_str, amsgs = _to_anthropic(messages)
        if tools:
            sys_str = _augment_system(sys_str, _tool_system(tools))
        data = self._post(self.cfg.complete_url, sys_str, amsgs)
        return _parse_tool_calls(data.get("text") or "")

    def chat_stream(self, messages: list[dict], *, tools: list[dict] | None = None):
        """최종 대화 응답 스트리밍. 토큰 델타(str)를 순차 yield. 미구성이면 RuntimeError.

        펑션콜 단계는 `chat`(논스트리밍)에서 처리하고, 도구가 소진된 최종 답변만 이 경로로 스트리밍한다.
        (turn_stream 은 tools=None 로 호출하지만, 방어적으로 shim 지시도 반영한다.)
        """
        self._require()
        sys_str, amsgs = _to_anthropic(messages)
        if tools:
            sys_str = _augment_system(sys_str, _tool_system(tools))
        payload = _payload(sys_str, amsgs, self.cfg)
        with httpx.stream("POST", self.cfg.stream_url, json=payload, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()
            yield from _iter_sse_deltas(resp.iter_lines())

    # ── 내부 ──────────────────────────────────────────────────────────────
    def _require(self) -> None:
        if not self.available:
            raise RuntimeError("LLM 미구성 (complete_url/stream_url 확인)")

    def _post(self, url: str, system: str, messages: list[dict]) -> dict:
        resp = httpx.post(url, json=_payload(system, messages, self.cfg), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()


# ── 순수 함수 (네트워크 없음 · 단위 테스트 대상) ─────────────────────────────


def _payload(system: str, messages: list[dict], cfg: LLMConfig) -> dict:
    """엔드포인트 요청 바디. temperature/top_p/top_k 는 넣지 않는다(Sonnet 5 제약)."""
    return {
        "system": system,
        "messages": messages,
        "max_tokens": cfg.max_tokens,
        "effort": cfg.effort,
    }


def _augment_system(base: str, extra: str) -> str:
    return (base + "\n\n" + extra) if base else extra


def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 와이어 메시지 → (system 문자열, Anthropic 스타일 messages).

    - system 메시지: system 문자열로 추출(여러 개면 이어 붙임).
    - assistant + tool_calls: 그 호출을 `{"tool_calls":[...]}` JSON 텍스트로 렌더(shim 왕복).
    - tool(도구 결과): user 텍스트로 렌더(`도구 결과(<id>): …`).
    - 연속 동일 role 은 하나의 메시지로 병합(Anthropic 교대 규칙 만족).
    """
    system_parts: list[str] = []
    conv: list[tuple[str, str]] = []  # (role, text)

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(_as_text(m["content"]))
        elif role == "tool":
            tid = m.get("tool_call_id", "")
            conv.append(("user", f"도구 결과({tid}):\n{_as_text(m.get('content'))}"))
        elif role == "assistant":
            tcs = m.get("tool_calls")
            if tcs:
                calls = [
                    {"name": c["function"]["name"],
                     "arguments": _loads(c["function"].get("arguments"))}
                    for c in tcs
                ]
                conv.append(("assistant", json.dumps({"tool_calls": calls}, ensure_ascii=False)))
            else:
                conv.append(("assistant", _as_text(m.get("content"))))
        else:  # user
            conv.append(("user", _as_text(m.get("content"))))

    # 연속 동일 role 병합.
    merged: list[tuple[str, str]] = []
    for role, text in conv:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n\n" + text)
        else:
            merged.append((role, text))

    amsgs = [{"role": r, "content": [{"type": "text", "text": t}]} for r, t in merged]
    return "\n\n".join(p for p in system_parts if p), amsgs


def _tool_system(tools: list[dict]) -> str:
    """OpenAI function 스키마 → shim 지시 프롬프트(도구 목록 + JSON 출력 규약)."""
    lines = ["사용 가능한 도구:"]
    for t in tools:
        fn = t.get("function", t)
        params = json.dumps(fn.get("parameters", {}), ensure_ascii=False)
        lines.append(f"- {fn.get('name')}: {fn.get('description', '')}\n  파라미터 스키마: {params}")
    lines.append(
        "\n도구를 호출하려면 다른 어떤 텍스트도 없이 아래 JSON 만 출력하라:\n"
        '{"tool_calls":[{"name":"<도구명>","arguments":{<인자 객체>}}]}\n'
        "여러 도구를 동시에 호출하려면 tool_calls 배열에 항목을 추가한다.\n"
        "도구가 더 필요 없으면 사용자에게 일반 텍스트로 답하라(JSON 없이)."
    )
    return "\n".join(lines)


def _parse_tool_calls(text: str) -> dict:
    """응답 text 에서 `{"tool_calls":[...]}` 를 파싱. 없으면 일반 텍스트 답변으로 폴백.

    반환 계약은 OpenAI 경로와 동일: {"content": str|None, "tool_calls": [{"id","name","arguments"}]}.
    id 는 shim 이 합성한다(call_1, call_2 …). 관대하게: 코드펜스 제거 + {…} 부분 추출.
    """
    obj = _extract_toolcalls_obj(text)
    if obj is not None:
        calls = []
        for i, c in enumerate(obj.get("tool_calls") or [], 1):
            if isinstance(c, dict) and c.get("name"):
                args = c.get("arguments")
                calls.append({"id": f"call_{i}", "name": c["name"],
                              "arguments": args if isinstance(args, dict) else {}})
        if calls:
            return {"content": None, "tool_calls": calls}
    return {"content": text, "tool_calls": []}


def _extract_toolcalls_obj(text: str) -> dict | None:
    """text 에서 tool_calls 를 담은 dict 를 뽑아낸다(직접 JSON → 코드펜스 → {…} 슬라이스 순)."""
    candidates = [_strip_code_fence(text.strip())]
    stripped = candidates[0]
    # {…} 슬라이스 폴백(프로즈에 섞인 경우).
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
            return obj
    return None


def _strip_code_fence(text: str) -> str:
    """```json … ``` / ``` … ``` 코드펜스를 벗겨 안쪽만 반환(펜스 없으면 원문)."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _iter_sse_deltas(lines):
    """SSE 라인 이터러블 → delta 문자열 시퀀스. `type=="done"` 에서 종료, meta 등은 무시."""
    for line in lines:
        if not line:
            continue
        payload = line[5:].strip() if line.startswith("data:") else line.strip()
        if not payload:
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype == "delta":
            delta = evt.get("delta")
            if delta:
                yield delta
        elif etype == "done":
            return


def _as_text(content) -> str:
    """content 를 평문으로. str 이면 그대로, Anthropic 블록 리스트면 text 만 이어 붙임."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    return str(content)


def _loads(raw) -> dict:
    """tool_calls arguments(직렬화 문자열) → dict. 실패 시 빈 dict."""
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
