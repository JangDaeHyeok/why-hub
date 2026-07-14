"""LLM 클라이언트 — OpenAI 호환 래퍼 (curate·요약·AI 생성). 호스팅 API로 시작 (CLAUDE.md §3).

미구성(base_url/model/api key 없음) 시 `available` 이 False 이며, 소비자는 **graceful skip** 한다.
openai 패키지는 실제 호출 시점에만 lazy import 한다(미설치·미구성 환경에서도 코어가 동작).

구현 Phase: P12.
"""

from __future__ import annotations

import os

from .config import LLMConfig


class LLMUnavailable(RuntimeError):
    """LLM 미구성 — 해당 기능(예: /generate)은 비활성."""


class LLMClient:
    """OpenAI 호환 chat completion 클라이언트(얇은 래퍼)."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    @property
    def available(self) -> bool:
        """base_url·model·api key 가 모두 갖춰졌을 때만 True."""
        return bool(
            self.cfg.base_url
            and self.cfg.model
            and os.environ.get(self.cfg.api_key_env)
        )

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """chat completion. 미구성이면 RuntimeError — 호출 전 `available` 로 가드할 것."""
        if not self.available:
            raise RuntimeError("LLM 미구성 (base_url/model/api key 확인)")
        from openai import OpenAI  # lazy import

        client = OpenAI(
            base_url=self.cfg.base_url,
            api_key=os.environ[self.cfg.api_key_env],
        )
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(model=self.cfg.model, messages=messages)
        return resp.choices[0].message.content or ""

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None) -> dict:
        """멀티턴 chat completion (function-calling 지원). 미구성이면 RuntimeError.

        messages 는 OpenAI 와이어 포맷(system/user/assistant/tool). tools 전달 시 도구 호출 활성.
        반환: {"content": str|None, "tool_calls": [{"id", "name", "arguments"(dict)}]}.
        arguments 는 파싱된 dict — 오케스트레이터가 재적재 시 다시 직렬화한다.
        """
        if not self.available:
            raise RuntimeError("LLM 미구성 (base_url/model/api key 확인)")
        import json

        from openai import OpenAI  # lazy import

        client = OpenAI(
            base_url=self.cfg.base_url,
            api_key=os.environ[self.cfg.api_key_env],
        )
        # 펑션콜(도구 해결) 단계 — stream=False 로 tool_calls 를 온전히 받는다.
        kwargs: dict = {"model": self.cfg.model, "messages": messages, "stream": False}
        if tools:
            kwargs["tools"] = tools
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        return {"content": msg.content, "tool_calls": calls}

    def chat_stream(self, messages: list[dict], *, tools: list[dict] | None = None):
        """최종 대화 응답 스트리밍 (stream=True). 토큰 델타(str)를 순차 yield. 미구성이면 RuntimeError.

        펑션콜 단계는 `chat`(stream=False)에서 처리하고, 도구가 소진된 최종 답변만 이 경로로 스트리밍한다.
        """
        if not self.available:
            raise RuntimeError("LLM 미구성 (base_url/model/api key 확인)")
        from openai import OpenAI  # lazy import

        client = OpenAI(
            base_url=self.cfg.base_url,
            api_key=os.environ[self.cfg.api_key_env],
        )
        kwargs: dict = {"model": self.cfg.model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools
        for chunk in client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content
