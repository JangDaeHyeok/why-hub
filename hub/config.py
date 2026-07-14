"""설정 — 상수 하드코딩 대신 config (CLAUDE.md §6).

저장소 루트 · 타입별 id 정규식 · 섹션 별칭 · 락 타임아웃 · LLM 옵션을 담는다.
TOML 파일에서 로드할 수 있다(파일 없으면 기본값).

부작용은 파일 읽기(로드)뿐이며, 그 외 로직은 순수하다.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import DOC_TYPES

# 타입별 id 정규식 기본값 ([Δ] §4: `^[a-z]+-[0-9]{4}$`).
# 타입별로 다른 규칙을 줄 수 있도록 매핑으로 열어둔다.
DEFAULT_ID_PATTERNS: dict[str, str] = {t: r"^[a-z]+-[0-9]{4}$" for t in DOC_TYPES}

# ADR 필수 섹션의 별칭 ([Δ] §4-3: "결과"≈"결과 및 영향").
# 표준 섹션명 -> 허용 별칭 목록.
DEFAULT_SECTION_ALIASES: dict[str, list[str]] = {
    "배경": ["배경", "맥락", "context"],
    "결정": ["결정", "decision"],
    "근거": ["근거", "이유", "rationale"],
    "대안": ["대안", "고려한 대안", "폐기 선택지", "alternatives"],
    "결과": ["결과", "결과 및 영향", "영향", "consequences"],
}

# ADR 필수 섹션 (표준명) — lint 게이트가 강제 (CLAUDE.md §2-5).
ADR_REQUIRED_SECTIONS = ("배경", "결정", "근거", "대안", "결과")


@dataclass
class LLMConfig:
    """OpenAI 호환 클라이언트 설정 (curate·요약·AI 생성). 옵션 — 미구성 시 graceful skip."""

    base_url: str | None = None
    model: str | None = None
    api_key_env: str = "OPENAI_API_KEY"


@dataclass
class Config:
    """지식 허브 런타임 설정."""

    repo_root: Path = field(default_factory=lambda: Path("knowledge"))
    id_patterns: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_ID_PATTERNS)
    )
    section_aliases: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_SECTION_ALIASES.items()}
    )
    adr_required_sections: tuple[str, ...] = ADR_REQUIRED_SECTIONS
    lock_timeout: float = 10.0
    llm: LLMConfig = field(default_factory=LLMConfig)

    def id_pattern(self, doc_type: str) -> str:
        """해당 타입의 id 정규식. 미정의 타입은 기본 패턴으로 폴백."""
        return self.id_patterns.get(doc_type, r"^[a-z]+-[0-9]{4}$")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """TOML 설정 파일에서 로드. path 가 None 이거나 파일이 없으면 기본값."""
        cfg = cls()
        if path is None:
            return cfg
        p = Path(path)
        if not p.exists():
            return cfg

        with p.open("rb") as f:
            data = tomllib.load(f)

        if "repo_root" in data:
            cfg.repo_root = Path(data["repo_root"])
        if "lock_timeout" in data:
            cfg.lock_timeout = float(data["lock_timeout"])
        if "id_patterns" in data:
            cfg.id_patterns.update(data["id_patterns"])
        if "section_aliases" in data:
            cfg.section_aliases.update(data["section_aliases"])
        if "adr_required_sections" in data:
            cfg.adr_required_sections = tuple(data["adr_required_sections"])
        if "llm" in data:
            llm = data["llm"]
            cfg.llm = LLMConfig(
                base_url=llm.get("base_url"),
                model=llm.get("model"),
                api_key_env=llm.get("api_key_env", "OPENAI_API_KEY"),
            )
        return cfg
