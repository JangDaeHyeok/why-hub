"""MCP 서버 — 읽기 도구 (FastMCP) ([기획안1 §11], 기획안2 §1 시그니처 유지).

에이전트가 쓰는 읽기 도구 4종(+ get_docs_diff)을 노출한다. 각 도구는 **service 호출만** 하고
반환에 **출처(id + anchor)** 를 포함한다. 쓰기 도구(save_document)는 P09 에서 추가.

구현 Phase: P07.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..config import Config
from ..service import KnowledgeService
from ..store.lint import LintError


def build_mcp(service: KnowledgeService) -> FastMCP:
    """주어진 서비스 위에 MCP 읽기 도구를 등록한 서버를 만든다(테스트·구동 공용)."""
    mcp = FastMCP("knowledge-hub")

    @mcp.tool
    def search_knowledge(query: str, filters: dict | None = None, k: int = 10) -> list[dict]:
        """유사 RAG 검색 — 필터 → FTS(BM25). 결과에 출처(doc_id + anchor) 포함."""
        return service.search_knowledge(query, filters, k)

    @mcp.tool
    def get_document(id: str) -> dict | None:
        """문서 원문 + frontmatter + 앵커 목록. 없으면 null."""
        return service.get_document(id)

    @mcp.tool
    def list_documents(
        type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """문서 목록 — type/status/tags 필터."""
        filters = {
            k: v
            for k, v in (("type", type), ("status", status), ("tags", tags))
            if v is not None
        }
        return service.list_documents(filters, limit=limit, offset=offset)

    @mcp.tool
    def get_history(id: str, anchor: str | None = None, limit: int | None = None) -> list[dict]:
        """변경 이력(delta/summary/actor) 타임라인. anchor 필터 지원."""
        return service.get_history(id, anchor=anchor, limit=limit)

    @mcp.tool
    def get_docs_diff(id: str, date: str | None = None) -> list[dict]:
        """의도된 변경(docs-diff) 조회."""
        return service.get_docs_diff(id, date=date)

    @mcp.tool
    def get_related(id: str) -> dict | None:
        """계보 — supersedes 체인(정방향/역방향) + related 양방향. 없으면 null."""
        return service.get_related(id)

    @mcp.tool
    def save_document(
        markdown: str,
        actor: str = "agent",
        change_type: str | None = None,
        intended_diff: str | None = None,
    ) -> dict:
        """문서 저장 — save 루틴 경유(정규화·lint·delta·이력·재색인).

        lint 실패 시 저장하지 않고 사유를 에러로 반환한다(CLAUDE.md §2-4).
        """
        try:
            res = service.save_document(
                markdown,
                actor=actor,
                change_type=change_type,
                intended_diff=intended_diff,
            )
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        return asdict(res)

    @mcp.tool
    def ingest_source(
        source_ref: str,
        content: str,
        doc_type: str = "reference",
        title: str | None = None,
        actor: str = "ingest",
    ) -> dict:
        """소스를 save 경유로 인제스트. 같은 source_ref 재입력 시 갱신(멱등)."""
        try:
            res = service.ingest_source(
                source_ref, content=content, actor=actor, doc_type=doc_type, title=title
            )
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        return asdict(res)

    @mcp.tool
    def curate(query: str, candidate_ids: list[str]) -> dict:
        """후보 문서를 LLM 으로 압축 요약. LLM 미구성 시 skip."""
        return service.curate(query, candidate_ids)

    return mcp


def main() -> None:  # pragma: no cover - 구동 진입점
    """`python -m hub.interfaces.mcp_server` 로 stdio MCP 서버 구동."""
    config = Config()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    service = KnowledgeService(root, config)
    build_mcp(service).run()


if __name__ == "__main__":  # pragma: no cover
    main()
