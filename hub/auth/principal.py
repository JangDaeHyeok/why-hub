"""Principal — 인터페이스 독립 인증 주체 + 공유 authorization policy (구현스펙-인증인가-RBAC.md §2).

HTTP/UI(세션쿠키)·MCP(JWT)가 각자 인증 결과를 `Principal` 로 변환하고, service·인터페이스가
동일한 `require_scope()` 로 인가한다. FastAPI/FastMCP 객체를 service 에 넘기지 않는다(경계 분리).
"""

from __future__ import annotations

from dataclasses import dataclass

# ── scope (최종 권한 판정 기준 · §2-3) ──────────────────────────────────
SCOPE_READ = "knowledge:read"
SCOPE_SUBMIT = "knowledge:submit"
SCOPE_REVIEW = "knowledge:review"

MEMBER_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_SUBMIT)
ADMIN_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_SUBMIT, SCOPE_REVIEW)
ALL_SCOPES: tuple[str, ...] = ADMIN_SCOPES

# 프로젝트별 역할 (프로젝트 ACL). viewer=읽기, editor=읽기+제출.
PROJECT_VIEWER = "viewer"
PROJECT_EDITOR = "editor"


def scopes_for(is_admin: bool) -> list[str]:
    """역할(is_admin) → scope 목록. admin 이면 review scope 추가(§2-3)."""
    return list(ADMIN_SCOPES if is_admin else MEMBER_SCOPES)


@dataclass(frozen=True)
class Principal:
    """인증된 주체. `scopes`(전역) + `projects`(프로젝트별 역할)가 권한 판정 기준.

    `projects` 는 (slug, role) 튜플의 정렬된 튜플(frozen — 해시 가능). username/is_admin 은 표시·감사용.
    admin 은 모든 프로젝트 전권(ACL 우회).
    """

    user_id: str
    username: str
    is_admin: bool = False
    scopes: tuple[str, ...] = ()
    projects: tuple[tuple[str, str], ...] = ()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    # ── 프로젝트 ACL (공유 policy — HTTP/UI/MCP/service 공통) ───────────
    def project_role(self, slug: str, default_project: str) -> str | None:
        """이 주체의 프로젝트 역할. admin→editor(전권), 기본 프로젝트→editor(모든 member 공개),
        아니면 멤버십 role. 접근권 없으면 None."""
        if self.is_admin:
            return PROJECT_EDITOR
        if slug == default_project:
            return PROJECT_EDITOR
        for s, role in self.projects:
            if s == slug:
                return role
        return None

    def can_read(self, slug: str, default_project: str) -> bool:
        return self.project_role(slug, default_project) is not None

    def can_write(self, slug: str, default_project: str) -> bool:
        return (
            SCOPE_SUBMIT in self.scopes
            and self.project_role(slug, default_project) == PROJECT_EDITOR
        )

    def readable_projects(self, default_project: str) -> set[str] | None:
        """읽기 허용 프로젝트 집합. admin→None(전체 — 필터 생략). 아니면 {기본} ∪ 멤버십 slug."""
        if self.is_admin:
            return None
        return {default_project} | {s for s, _ in self.projects}

    @classmethod
    def for_user(
        cls,
        username: str,
        *,
        user_id: str | None = None,
        is_admin: bool = False,
        scopes: list[str] | tuple[str, ...] | None = None,
        projects: dict[str, str] | None = None,
    ) -> "Principal":
        """역할 기반 Principal 생성(세션/JWT 변환·테스트 공용). scopes 미지정 시 역할로 도출."""
        sc = tuple(scopes) if scopes is not None else tuple(scopes_for(is_admin))
        pj = tuple(sorted((projects or {}).items()))
        return cls(user_id=user_id or username, username=username, is_admin=is_admin,
                   scopes=sc, projects=pj)

    @classmethod
    def system(cls, username: str = "system") -> "Principal":
        """오프라인 도구(seed/import) 전용 전권 주체 — 인터페이스로는 절대 만들지 않는다."""
        return cls(user_id=username, username=username, is_admin=True, scopes=ALL_SCOPES)


def require_scope(principal: Principal | None, scope: str) -> None:
    """principal 에 scope 가 없으면 PermissionError(→ HTTP 403 / MCP ToolError). 공유 policy."""
    if principal is None or scope not in principal.scopes:
        who = getattr(principal, "username", None) or "anonymous"
        raise PermissionError(f"권한 없음: '{scope}' 필요 (주체 {who})")


def require_read(principal: Principal, slug: str, default_project: str) -> None:
    if not principal.can_read(slug, default_project):
        raise PermissionError(f"프로젝트 읽기 권한 없음: '{slug}' (주체 {principal.username})")


def require_write(principal: Principal, slug: str, default_project: str) -> None:
    if not principal.can_write(slug, default_project):
        raise PermissionError(f"프로젝트 쓰기 권한 없음: '{slug}' (주체 {principal.username})")
