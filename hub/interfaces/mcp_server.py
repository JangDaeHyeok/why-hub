"""MCP 서버 — 지식 도구 (FastMCP) + JWT 인증/인가 (구현스펙-인증인가-RBAC.md).

각 도구는 **service 호출만** 하고 반환에 출처(id + anchor)를 포함한다. 인증은 Bearer JWT(RS256)로,
MCP 서버는 **public key/JWKS 로 stateless 검증만** 한다(private key·PAT pepper 미보유). 도구별 scope 를
`require_scopes` 로 선언하고, 토큰의 sub/scope 를 Principal 로 변환해 actor·권한을 결정한다.
actor/approver 인자는 제거됐다(사용자 입력 신뢰 금지). 인증 활성 + stdio 는 기동을 실패시킨다.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.server.dependencies import get_access_token

from ..auth.deps import LOCAL_PRINCIPAL
from ..auth.principal import (
    SCOPE_READ,
    SCOPE_REVIEW,
    SCOPE_SUBMIT,
    Principal,
)
from ..config import Config
from ..service import KnowledgeService
from ..store.lint import LintError


def _ser(res):
    return asdict(res) if is_dataclass(res) else res


def build_verifier(config: Config):
    """public key/JWKS 만으로 JWTVerifier 구성(private key 미보유). algorithm 은 RS256 고정."""
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    ac = config.auth
    common = dict(issuer=ac.issuer, audience=ac.mcp_audience, algorithm="RS256")
    if ac.jwks_url:
        return JWTVerifier(jwks_uri=ac.jwks_url, **common)
    if not ac.public_key_file:
        raise RuntimeError("인증 활성 — AUTH_PUBLIC_KEY_FILE 또는 AUTH_JWKS_URL 이 필요합니다.")
    pub = Path(ac.public_key_file).read_text(encoding="utf-8")
    return JWTVerifier(public_key=pub, **common)


def build_mcp(service: KnowledgeService, verifier=None) -> FastMCP:
    """verifier(JWTVerifier)가 주어지면 JWT 인증·도구별 scope 를 강제, None 이면 로컬(무인증) 모드.

    무인증 모드는 로컬/단위 테스트용 — 전권 로컬 주체로 동작한다.
    """
    mcp = FastMCP("knowledge-hub", auth=verifier) if verifier is not None else FastMCP("knowledge-hub")

    def _principal() -> Principal:
        """검증된 JWT → Principal. 무인증 모드면 로컬 전권 주체."""
        if verifier is None:
            return LOCAL_PRINCIPAL
        tok = get_access_token()
        claims = getattr(tok, "claims", None) or {}
        subject = getattr(tok, "subject", None) or claims.get("sub") or ""
        projects = claims.get("projects") or {}
        return Principal(
            user_id=subject,
            username=claims.get("username") or subject,
            is_admin=bool(claims.get("is_admin")),
            scopes=tuple(getattr(tok, "scopes", None) or []),
            projects=tuple(sorted(projects.items())),
        )

    def _tool(scope: str):
        """scope 선언 데코레이터 — 인증 모드에선 require_scopes, 무인증이면 그냥 등록."""
        return mcp.tool(auth=require_scopes(scope)) if verifier is not None else mcp.tool

    # ── 읽기 (knowledge:read) ─────────────────────────────────────────
    @_tool(SCOPE_READ)
    def search_knowledge(query: str, filters: dict | None = None, k: int = 10) -> list[dict]:
        """유사 RAG 검색 — 필터 → FTS(BM25). 결과에 출처(doc_id + anchor) 포함. 프로젝트 ACL 적용."""
        return service.search_knowledge(query, filters, k, principal=_principal())

    @_tool(SCOPE_READ)
    def get_document(id: str) -> dict | None:
        """문서 원문 + frontmatter + 앵커 목록. 없거나 접근 불가면 null."""
        return service.get_document(id, principal=_principal())

    @_tool(SCOPE_READ)
    def list_documents(
        type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """문서 목록 — type/status/tags/project 필터. 프로젝트 ACL 적용."""
        filters = {
            k: v
            for k, v in (("type", type), ("status", status),
                         ("project", project), ("tags", tags))
            if v is not None
        }
        return service.list_documents(filters, limit=limit, offset=offset, principal=_principal())

    @_tool(SCOPE_READ)
    def list_projects() -> list[str]:
        """접근 가능한 project 목록(멀티프로젝트 스코프용)."""
        return service.list_projects(principal=_principal())

    @_tool(SCOPE_READ)
    def get_history(id: str, anchor: str | None = None, limit: int | None = None) -> list[dict]:
        """변경 이력(delta/summary/actor) 타임라인. anchor 필터 지원. 접근 불가면 빈 목록."""
        return service.get_history(id, anchor=anchor, limit=limit, principal=_principal())

    @_tool(SCOPE_READ)
    def get_docs_diff(id: str, date: str | None = None) -> list[dict]:
        """의도된 변경(docs-diff) 조회. 접근 불가면 빈 목록."""
        return service.get_docs_diff(id, date=date, principal=_principal())

    @_tool(SCOPE_READ)
    def get_related(id: str) -> dict | None:
        """계보 — supersedes 체인(정방향/역방향) + related 양방향. 없거나 접근 불가면 null."""
        return service.get_related(id, principal=_principal())

    @_tool(SCOPE_READ)
    def curate(query: str, candidate_ids: list[str]) -> dict:
        """후보 문서를 LLM 으로 압축 요약. LLM 미구성 시 skip. 접근 가능 문서만."""
        return service.curate(query, candidate_ids, principal=_principal())

    # ── 쓰기 (knowledge:submit) — actor 는 인증 주체 ───────────────────
    @_tool(SCOPE_SUBMIT)
    def save_document(
        markdown: str,
        intended_diff: str | None = None,
        project: str | None = None,
    ) -> dict:
        """문서 저장 — save 루틴 경유. actor 는 JWT 주체(인자로 받지 않음). 승인 게이트 on 이면 제출.

        change_type 은 받지 않는다 — 저장 엔진이 자동 판정(클라이언트 이력 타입 위조 차단)."""
        p = _principal()
        try:
            res = service.save_document(
                markdown,
                actor=p.username,
                intended_diff=intended_diff,
                project=project,
                principal=p,
            )
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        except PermissionError as e:
            raise ToolError(f"프로젝트 쓰기 권한 없음: {e}") from e
        return _ser(res)

    @_tool(SCOPE_SUBMIT)
    def ingest_source(
        source_ref: str,
        content: str,
        doc_type: str = "reference",
        title: str | None = None,
        project: str | None = None,
    ) -> dict:
        """소스를 save 경유로 인제스트(멱등). actor 는 JWT 주체. 승인 게이트 on 이면 제출."""
        p = _principal()
        try:
            res = service.ingest_source(
                source_ref, content=content, actor=p.username,
                doc_type=doc_type, title=title, project=project, principal=p,
            )
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        except PermissionError as e:
            raise ToolError(f"프로젝트 쓰기 권한 없음: {e}") from e
        return _ser(res)

    @_tool(SCOPE_SUBMIT)
    def list_submissions(status: str | None = None, project: str | None = None) -> list[dict]:
        """제출 목록. admin(review)은 전체, member 는 본인 제출만(§2.1)."""
        principal = _principal()
        subs = service.list_submissions(status, project=project)
        if not principal.has_scope(SCOPE_REVIEW):
            subs = [s for s in subs if s.get("actor") == principal.username]
        return subs

    # ── 승인 (knowledge:review — admin) ───────────────────────────────
    @_tool(SCOPE_REVIEW)
    def approve_submission(sub_id: str) -> dict:
        """제출 승인(실제 반영). review scope(admin)만. approver 는 JWT 주체(인자로 받지 않음)."""
        try:
            res = service.approve_submission(sub_id, principal=_principal())
        except PermissionError as e:
            raise ToolError(f"승인 권한 없음: {e}") from e
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e
        return asdict(res)

    @_tool(SCOPE_REVIEW)
    def reject_submission(sub_id: str, note: str = "") -> dict:
        """제출 반려(반영 없음). review scope(admin)만."""
        try:
            return service.reject_submission(sub_id, principal=_principal(), note=note)
        except PermissionError as e:
            raise ToolError(f"반려 권한 없음: {e}") from e
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    return mcp


def main() -> None:  # pragma: no cover - 구동 진입점
    """`python -m hub.interfaces.mcp_server` 로 MCP 서버 구동.

    인증 활성 상태에서 stdio transport 는 거부한다(무인증 노출 방지 — 배포는 streamable-http 강제).
    """
    config = Config.load_default()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    transport = os.environ.get("KNOWLEDGE_HUB_MCP_TRANSPORT", "stdio")

    if config.auth.enabled and transport == "stdio":
        raise SystemExit(
            "인증이 활성화된 상태에서는 stdio transport 를 사용할 수 없습니다 — "
            "streamable-http 로 구동하세요 (KNOWLEDGE_HUB_MCP_TRANSPORT=streamable-http)."
        )

    service = KnowledgeService(root, config)
    verifier = build_verifier(config) if config.auth.enabled else None
    mcp = build_mcp(service, verifier)
    if transport == "stdio":
        mcp.run()
    else:
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8001"))
        mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
