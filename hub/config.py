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
    """LLM 엔드포인트 설정 (curate·요약·AI 생성·멀티턴 채팅). 옵션 — 미구성 시 graceful skip.

    커스텀 HTTP 엔드포인트(Anthropic Messages 스타일 · 스트리밍/논스트리밍 2종)를 호출한다.
    공개 URL 이라 api key 불필요. 네이티브 펑션콜은 없고 — 툴 사용은 프롬프트 shim(llm.py)로 흡수한다.
    """

    complete_url: str | None = None  # 논스트리밍 엔드포인트 (complete·chat)
    stream_url: str | None = None    # 스트리밍 엔드포인트 (chat_stream, SSE)
    effort: str = "high"             # low | high (엔드포인트 default high)
    max_tokens: int = 4096


@dataclass
class PostgresConfig:
    """PostgreSQL 접속 (배포 백엔드). dsn 우선, 없으면 host/port/... 로 조립. 비밀번호는 env 에서."""

    dsn: str | None = None
    host: str = "localhost"
    port: int = 5432
    database: str = "knowledge_hub"
    user: str = "hub"
    password_env: str = "PGPASSWORD"
    # 스키마 DDL(CREATE/ALTER) 을 이 프로세스가 실행할지. **소유 롤(hub_admin)만 True**.
    # 검증 전용 롤(hub_mcp)은 False → DDL 을 실행하지 않고 스키마 준비를 대기·확인만 한다
    # (기동 순서 의존 크래시·소유권 충돌 방지, 구현스펙-인증인가-RBAC.md §8).
    manage_schema: bool = True

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

    enabled=True 면 모든 쓰기(수동·AI·ingest)가 승인 대기 큐를 거친다. 승인/반려는 **knowledge:review
    scope(=users.is_admin)** 보유자만 가능하다(과거 admins 문자열 목록은 제거 — 구현스펙-인증인가-RBAC.md §2-4).
    enabled=False 면 기존처럼 즉시 반영(하위호환·seed·store 테스트).

    **코드 기본값은 False**(opt-in) — 기존 동작·시드·단위 테스트를 깨지 않는다.
    """

    enabled: bool = False


@dataclass
class AuthConfig:
    """인증/인가 설정 (구현스펙-인증인가-RBAC.md §9).

    **secret(private key·PAT pepper·session secret)은 여기 두지 않는다** — env/secret 파일로 주입한다.
    이 데이터클래스는 비시크릿 설정(플래그·issuer·TTL·키파일 경로·env 변수명)만 담는다.
    코드 기본값은 enabled=False(로컬/테스트 무마찰); 배포는 config/env 로 켠다.
    """

    enabled: bool = False
    issuer: str = "why-hub"
    mcp_audience: str = "why-hub-mcp"
    access_token_ttl_seconds: int = 600  # JWT 기본 TTL 10분
    session_ttl_seconds: int = 1209600   # 웹 세션 14일
    cookie_name: str = "wh_session"
    cookie_secure: bool = True           # 배포 true, 로컬 개발은 false 허용
    signup_enabled: bool = True
    # rate limit 임계값(상수 하드코딩 대신 config, CLAUDE.md §6). 로그인은 계정·클라이언트 두 버킷
    # (사용자명 회전 우회 차단 — 클라이언트 버킷은 NAT 다중 사용자 대비 넉넉히). 가입은 클라이언트 버킷.
    login_rate_window_seconds: int = 60
    login_max_per_account: int = 10
    login_max_per_client: int = 30
    signup_rate_window_seconds: int = 3600
    signup_max_per_client: int = 10
    exchange_rate_window_seconds: int = 60
    exchange_max_per_client: int = 20
    # 키·시크릿은 값이 아니라 위치만 config 에(주입 경로).
    private_key_file: str | None = None  # admin 서버만
    public_key_file: str | None = None   # MCP 검증용 (또는 jwks_url)
    jwks_url: str | None = None
    pat_pepper_env: str = "AUTH_PAT_PEPPER"
    session_secret_env: str = "AUTH_SESSION_SECRET"


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
    auth: AuthConfig = field(default_factory=AuthConfig)

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
                manage_schema=bool(pg.get("manage_schema", True)),
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
                complete_url=llm.get("complete_url"),
                stream_url=llm.get("stream_url"),
                effort=str(llm.get("effort", "high")),
                max_tokens=int(llm.get("max_tokens", 4096)),
            )
        if "approval" in data:
            appr = data["approval"]
            cfg.approval = ApprovalConfig(enabled=bool(appr.get("enabled", True)))
        if "auth" in data:
            a = data["auth"]
            cfg.auth = AuthConfig(
                enabled=bool(a.get("enabled", cfg.auth.enabled)),
                issuer=str(a.get("issuer", cfg.auth.issuer)),
                mcp_audience=str(a.get("mcp_audience", cfg.auth.mcp_audience)),
                access_token_ttl_seconds=int(
                    a.get("access_token_ttl_seconds", cfg.auth.access_token_ttl_seconds)
                ),
                session_ttl_seconds=int(
                    a.get("session_ttl_seconds", cfg.auth.session_ttl_seconds)
                ),
                cookie_name=str(a.get("cookie_name", cfg.auth.cookie_name)),
                cookie_secure=bool(a.get("cookie_secure", cfg.auth.cookie_secure)),
                signup_enabled=bool(a.get("signup_enabled", cfg.auth.signup_enabled)),
                private_key_file=a.get("private_key_file", cfg.auth.private_key_file),
                public_key_file=a.get("public_key_file", cfg.auth.public_key_file),
                jwks_url=a.get("jwks_url", cfg.auth.jwks_url),
                pat_pepper_env=str(a.get("pat_pepper_env", cfg.auth.pat_pepper_env)),
                session_secret_env=str(
                    a.get("session_secret_env", cfg.auth.session_secret_env)
                ),
                login_rate_window_seconds=int(
                    a.get("login_rate_window_seconds", cfg.auth.login_rate_window_seconds)
                ),
                login_max_per_account=int(
                    a.get("login_max_per_account", cfg.auth.login_max_per_account)
                ),
                login_max_per_client=int(
                    a.get("login_max_per_client", cfg.auth.login_max_per_client)
                ),
                signup_rate_window_seconds=int(
                    a.get("signup_rate_window_seconds", cfg.auth.signup_rate_window_seconds)
                ),
                signup_max_per_client=int(
                    a.get("signup_max_per_client", cfg.auth.signup_max_per_client)
                ),
                exchange_rate_window_seconds=int(
                    a.get("exchange_rate_window_seconds", cfg.auth.exchange_rate_window_seconds)
                ),
                exchange_max_per_client=int(
                    a.get("exchange_max_per_client", cfg.auth.exchange_max_per_client)
                ),
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
        cfg = cls.load(path)

        # LLM 엔드포인트 URL 은 인증 없는 공개 URL(=시크릿성)이라 git 커밋 config 에 두지 않고
        # 환경변수로 주입한다. env 가 있으면 파일 값을 덮어쓴다(env 우선, 없으면 파일값/미구성).
        complete = os.environ.get("KNOWLEDGE_HUB_LLM_COMPLETE_URL")
        stream = os.environ.get("KNOWLEDGE_HUB_LLM_STREAM_URL")
        if complete:
            cfg.llm.complete_url = complete
        if stream:
            cfg.llm.stream_url = stream

        # PostgreSQL DSN env 오버라이드 — admin/mcp 컨테이너가 서로 다른 DB 롤을 쓰도록
        # (배포 롤 분리, 구현스펙-인증인가-RBAC.md §8). 값이 있으면 config 의 host/user 무시.
        pg_dsn = os.environ.get("KNOWLEDGE_HUB_PG_DSN")
        if pg_dsn:
            cfg.postgres.dsn = pg_dsn

        # 스키마 소유 여부(DDL 실행) — 검증 전용 롤(mcp)은 false 로 주입해 DDL 을 돌리지 않는다.
        cfg.postgres.manage_schema = _env_bool(
            "KNOWLEDGE_HUB_PG_MANAGE_SCHEMA", cfg.postgres.manage_schema
        )

        # 인증 설정 env 레이어링 (secret 은 여기서 값이 아니라 위치만 — service 가 파일/env 로 읽음).
        cfg.auth = _auth_from_env(cfg.auth)
        return cfg


def _env_bool(name: str, default: bool) -> bool:
    v = __import__("os").environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _auth_from_env(base: "AuthConfig") -> "AuthConfig":
    """AUTH_* env 를 AuthConfig 위에 덮는다(env 우선). 파일값/기본값은 유지."""
    import os

    return AuthConfig(
        enabled=_env_bool("AUTH_ENABLED", base.enabled),
        issuer=os.environ.get("AUTH_ISSUER", base.issuer),
        mcp_audience=os.environ.get("AUTH_MCP_AUDIENCE", base.mcp_audience),
        access_token_ttl_seconds=int(
            os.environ.get("AUTH_ACCESS_TOKEN_TTL_SECONDS", base.access_token_ttl_seconds)
        ),
        session_ttl_seconds=int(
            os.environ.get("AUTH_SESSION_TTL_SECONDS", base.session_ttl_seconds)
        ),
        cookie_name=base.cookie_name,
        cookie_secure=_env_bool("AUTH_COOKIE_SECURE", base.cookie_secure),
        signup_enabled=_env_bool("AUTH_SIGNUP_ENABLED", base.signup_enabled),
        private_key_file=os.environ.get("AUTH_PRIVATE_KEY_FILE", base.private_key_file),
        public_key_file=os.environ.get("AUTH_PUBLIC_KEY_FILE", base.public_key_file),
        jwks_url=os.environ.get("AUTH_JWKS_URL", base.jwks_url),
        pat_pepper_env=base.pat_pepper_env,
        session_secret_env=base.session_secret_env,
        login_rate_window_seconds=base.login_rate_window_seconds,
        login_max_per_account=base.login_max_per_account,
        login_max_per_client=base.login_max_per_client,
        signup_rate_window_seconds=base.signup_rate_window_seconds,
        signup_max_per_client=base.signup_max_per_client,
        exchange_rate_window_seconds=base.exchange_rate_window_seconds,
        exchange_max_per_client=base.exchange_max_per_client,
    )
