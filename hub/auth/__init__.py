"""인증/인가(Auth · RBAC) 패키지 — 지식 Store 와 책임 분리 (구현스펙-인증인가-RBAC.md).

인증은 인터페이스(웹=세션쿠키, MCP=JWT)에서 처리하고, 결과를 인터페이스-독립 `Principal` 로
변환해 HTTP·UI·MCP·service 가 동일한 authorization policy(scope)를 쓴다. 이 패키지는 지식 문서
저장 불변식(save/reflect 단일 경로)에 영향을 주지 않는다.
"""

from .principal import (
    ADMIN_SCOPES,
    ALL_SCOPES,
    MEMBER_SCOPES,
    SCOPE_READ,
    SCOPE_REVIEW,
    SCOPE_SUBMIT,
    Principal,
    require_scope,
    scopes_for,
)

__all__ = [
    "Principal",
    "require_scope",
    "scopes_for",
    "SCOPE_READ",
    "SCOPE_SUBMIT",
    "SCOPE_REVIEW",
    "ALL_SCOPES",
    "MEMBER_SCOPES",
    "ADMIN_SCOPES",
]
