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
