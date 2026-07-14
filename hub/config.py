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
class PostgresConfig:
    """PostgreSQL 접속 (배포 백엔드). dsn 우선, 없으면 host/port/... 로 조립. 비밀번호는 env 에서."""

    dsn: str | None = None
    host: str = "localhost"
    port: int = 5432
    database: str = "knowledge_hub"
    user: str = "hub"
    password_env: str = "PGPASSWORD"

    def resolve_dsn(self) -> str:
        import os
        from urllib.parse import quote

        if self.dsn:
            return self.dsn
        pw = os.environ.get(self.password_env, "")
        # URI 예약문자(@ / # % : 등)가 user/password/db 에 있어도 안전하도록 각 컴포넌트를 인코딩.
        user = quote(self.user, safe="")
        db = quote(self.database, safe="")
        auth = f"{user}:{quote(pw, safe='')}@" if pw else f"{user}@"
        return f"postgresql://{auth}{self.host}:{self.port}/{db}"


@dataclass
class ApprovalConfig:
    """관리자 승인 워크플로우 설정 (구현스펙-승인워크플로우.md).

    enabled=True 면 모든 쓰기(수동·AI·ingest)가 승인 대기 큐를 거친다. admins 목록의
    actor 만 승인/반려할 수 있다. enabled=False 면 기존처럼 즉시 반영(하위호환·seed·store 테스트).

    **코드 기본값은 False**(opt-in) — 기존 동작·시드·단위 테스트를 깨지 않는다. 팀 배포는
    config.example.toml 처럼 `[approval] enabled = true` + admins 로 켠다.
    """

    enabled: bool = False
    admins: list[str] = field(default_factory=list)


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
    # 멀티프로젝트: project 미지정(frontmatter 없음/인덱스 NULL) 문서가 속하는 기본 프로젝트.
    default_project: str = "default"
    # 저장 백엔드: "file"(기본 · 로컬/테스트) | "postgres"(배포).
    storage: str = "file"
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)

    def id_pattern(self, doc_type: str) -> str:
        """해당 타입의 id 정규식. 미정의 타입은 기본 패턴으로 폴백."""
        return self.id_patterns.get(doc_type, r"^[a-z]+-[0-9]{4}$")

    def is_admin(self, actor: str | None) -> bool:
        """actor 가 승인 권한을 가진 관리자인지. (신원은 신뢰 기반 — 현 MVP 수준.)"""
        return bool(actor) and actor in self.approval.admins

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
        if "default_project" in data:
            cfg.default_project = str(data["default_project"])
        if "storage" in data:
            st = data["storage"]
            cfg.storage = str(st.get("backend", cfg.storage))
            pg = st.get("postgres", {})
            cfg.postgres = PostgresConfig(
                dsn=pg.get("dsn"),
                host=pg.get("host", "localhost"),
                port=int(pg.get("port", 5432)),
                database=pg.get("database", "knowledge_hub"),
                user=pg.get("user", "hub"),
                password_env=pg.get("password_env", "PGPASSWORD"),
            )
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
        if "approval" in data:
            appr = data["approval"]
            cfg.approval = ApprovalConfig(
                enabled=bool(appr.get("enabled", True)),
                admins=list(appr.get("admins", [])),
            )
        return cfg

    @classmethod
    def load_default(cls) -> "Config":
        """엔트리포인트용 기본 로드: `KNOWLEDGE_HUB_CONFIG` env → 없으면 `./config.toml` → 없으면 기본값.

        (멀티프로젝트·승인 등 config 기반 기능이 서버 구동 시 실제 반영되도록 한다.)
        """
        import os

        path = os.environ.get("KNOWLEDGE_HUB_CONFIG")
        if not path and Path("config.toml").exists():
            path = "config.toml"
        return cls.load(path)
