"""MCP 서버 — 읽기 도구 (FastMCP) ([기획안1 §11], 기획안2 §1 시그니처 유지).

에이전트가 쓰는 읽기 도구 4종(+ get_docs_diff)을 노출한다. 각 도구는 **service 호출만** 하고
반환에 **출처(id + anchor)** 를 포함한다. 쓰기 도구(save_document)는 P09 에서 추가.

구현 Phase: P07.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..config import Config
from ..service import KnowledgeService
from ..store.lint import LintError


def _ser(res):
    """SaveResult(데이터클래스) 또는 제출 dict 를 직렬화 가능 형태로."""
    return asdict(res) if is_dataclass(res) else res


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
        project: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """문서 목록 — type/status/tags/project 필터."""
        filters = {
            k: v
            for k, v in (("type", type), ("status", status),
                         ("project", project), ("tags", tags))
            if v is not None
        }
        return service.list_documents(filters, limit=limit, offset=offset)

    @mcp.tool
    def list_projects() -> list[str]:
        """인덱스에 존재하는 project 목록(멀티프로젝트 스코프용)."""
        return service.list_projects()

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
        project: str | None = None,
    ) -> dict:
        """문서 저장 — save 루틴 경유(정규화·lint·delta·이력·재색인).

        승인 게이트 on 이면 즉시 반영 대신 승인 대기 큐에 제출한다(제출 dict 반환).
        project 지정 시 그 프로젝트로 스코프(미지정=기본 프로젝트). lint 실패 시 사유를 에러로 반환.
        """
        try:
            res = service.save_document(
                markdown,
                actor=actor,
                change_type=change_type,
                intended_diff=intended_diff,
                project=project,
            )
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        return _ser(res)

    @mcp.tool
    def ingest_source(
        source_ref: str,
        content: str,
        doc_type: str = "reference",
        title: str | None = None,
        actor: str = "ingest",
        project: str | None = None,
    ) -> dict:
        """소스를 save 경유로 인제스트. 같은 source_ref(동일 project) 재입력 시 갱신(멱등).

        승인 게이트 on 이면 즉시 반영 대신 승인 대기 큐에 제출한다(제출 dict 반환).
        """
        try:
            res = service.ingest_source(
                source_ref, content=content, actor=actor, doc_type=doc_type,
                title=title, project=project,
            )
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        return _ser(res)

    @mcp.tool
    def curate(query: str, candidate_ids: list[str]) -> dict:
        """후보 문서를 LLM 으로 압축 요약. LLM 미구성 시 skip."""
        return service.curate(query, candidate_ids)

    # ── 승인 워크플로우 (구현스펙-승인워크플로우.md) ────────────────────
    @mcp.tool
    def list_submissions(status: str | None = None, project: str | None = None) -> list[dict]:
        """승인 대기/처리된 제출 목록(최신 먼저). status(pending|approved|rejected)·project 로 필터."""
        return service.list_submissions(status, project=project)

    @mcp.tool
    def approve_submission(sub_id: str, approver: str) -> dict:
        """제출을 승인해 실제 반영(save 루틴 경유). approver 는 config 관리자여야 한다.

        비관리자·lint 실패·이미 처리된 제출은 에러로 반환한다.
        """
        try:
            res = service.approve_submission(sub_id, approver=approver)
        except PermissionError as e:
            raise ToolError(f"승인 권한 없음: {e}") from e
        except LintError as e:
            raise ToolError("lint 실패: " + "; ".join(e.reasons)) from e
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e
        return asdict(res)

    @mcp.tool
    def reject_submission(sub_id: str, approver: str, note: str = "") -> dict:
        """제출을 반려(반영 없음). approver 는 config 관리자여야 한다."""
        try:
            return service.reject_submission(sub_id, approver=approver, note=note)
        except PermissionError as e:
            raise ToolError(f"반려 권한 없음: {e}") from e
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from e

    return mcp


def main() -> None:  # pragma: no cover - 구동 진입점
    """`python -m hub.interfaces.mcp_server` 로 stdio MCP 서버 구동."""
    config = Config.load_default()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    service = KnowledgeService(root, config)
    mcp = build_mcp(service)
    # 배포는 HTTP(streamable-http)로 네트워크 노출, 로컬은 stdio(기본).
    transport = os.environ.get("KNOWLEDGE_HUB_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8001"))
        mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
