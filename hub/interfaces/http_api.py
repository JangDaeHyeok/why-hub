"""HTTP API — 읽기 JSON 엔드포인트 (FastAPI) ([기획안1 §11] HTTP 열).

UI(M6~) 가 소비할 읽기 경로. 각 엔드포인트는 **service 호출만** 한다(로직 중복 없음).
에러 매핑: LintError→422, not found→404, LockTimeout→409.
CORS/정적 서빙/HTMX 는 UI Phase(M6~) — 지금은 순수 JSON.

구현 Phase: P08.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import Config
from ..llm import LLMUnavailable
from ..service import KnowledgeService
from ..store.lint import LintError
from ..store.locking import LockTimeout
from ..store.normalize import normalize


class SaveRequest(BaseModel):
    """PUT /docs/{id} 요청 바디. actor 는 인증에서 채워지는 값(MVP: 바디)."""

    markdown: str
    actor: str = "anonymous"
    change_type: str | None = None
    intended_diff: str | None = None


class IngestRequest(BaseModel):
    """POST /ingest 요청 바디. source_ref 로 멱등 갱신."""

    source_ref: str
    content: str
    doc_type: str = "reference"
    title: str | None = None
    actor: str = "ingest"


class GenerateRequest(BaseModel):
    """POST /generate 요청 바디 (AI 초안 — 저장 안 함)."""

    target_type: str = "adr"
    sources: list[dict] = []
    hint: str | None = None


def build_app(service: KnowledgeService) -> FastAPI:
    # 기본 Swagger UI(/docs)·ReDoc(/redoc) 을 끈다 — 기획안1 §11 의 `GET /docs`(목록)와 경로 충돌.
    app = FastAPI(title="knowledge-hub", version="0.1.0", docs_url=None, redoc_url=None)

    # ── 에러 매핑 (읽기엔 안 나지만 P09 쓰기가 상속) ──────────────────
    @app.exception_handler(LintError)
    async def _lint_error(request, exc: LintError):
        return JSONResponse(status_code=422, content={"error": "lint", "reasons": exc.reasons})

    @app.exception_handler(LockTimeout)
    async def _lock_timeout(request, exc: LockTimeout):
        return JSONResponse(status_code=409, content={"error": "lock_timeout", "detail": str(exc)})

    def _require(doc_id: str) -> dict:
        doc = service.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return doc

    # ── 검색 ──────────────────────────────────────────────────────────
    @app.get("/search")
    def search(
        q: str,
        type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = Query(None),
        k: int = 10,
    ):
        filters = {
            key: val
            for key, val in (("type", type), ("status", status), ("tags", tags))
            if val is not None
        }
        return service.search_knowledge(q, filters, k)

    # ── 목록 ──────────────────────────────────────────────────────────
    @app.get("/docs")
    def list_docs(
        type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = Query(None),
        limit: int | None = None,
        offset: int = 0,
    ):
        filters = {
            key: val
            for key, val in (("type", type), ("status", status), ("tags", tags))
            if val is not None
        }
        return service.list_documents(filters, limit=limit, offset=offset)

    # ── 조회 ──────────────────────────────────────────────────────────
    @app.get("/docs/{doc_id}")
    def get_doc(doc_id: str):
        return _require(doc_id)

    # ── 저장 (쓰기 — save 루틴 경유) ──────────────────────────────────
    @app.put("/docs/{doc_id}")
    def put_doc(doc_id: str, req: SaveRequest):
        # 경로 id 와 문서 frontmatter id 일치 검증.
        nd = normalize(req.markdown)
        if nd.id and nd.id != doc_id:
            raise HTTPException(
                status_code=422, detail=f"id 불일치: 경로 {doc_id} ≠ 문서 {nd.id}"
            )
        # LintError → 422(핸들러), LockTimeout → 409(핸들러).
        res = service.save_document(
            req.markdown,
            actor=req.actor,
            change_type=req.change_type,
            intended_diff=req.intended_diff,
        )
        return asdict(res)

    # ── 이력 ──────────────────────────────────────────────────────────
    @app.get("/docs/{doc_id}/history")
    def get_history(doc_id: str, anchor: str | None = None, limit: int | None = None):
        _require(doc_id)
        return service.get_history(doc_id, anchor=anchor, limit=limit)

    # ── 의도된 변경 ───────────────────────────────────────────────────
    @app.get("/docs/{doc_id}/diff")
    def get_diff(doc_id: str, date: str | None = None):
        _require(doc_id)
        return service.get_docs_diff(doc_id, date=date)

    # ── 계보 ──────────────────────────────────────────────────────────
    @app.get("/docs/{doc_id}/related")
    def get_related(doc_id: str):
        rel = service.get_related(doc_id)
        if rel is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return rel

    # ── 인제스천 (save 루틴 경유) ─────────────────────────────────────
    @app.post("/ingest")
    def ingest(req: IngestRequest):
        res = service.ingest_source(
            req.source_ref,
            content=req.content,
            actor=req.actor,
            doc_type=req.doc_type,
            title=req.title,
        )
        return asdict(res)

    # ── AI 생성 (초안 반환만, 저장 안 함) ─────────────────────────────
    @app.post("/generate")
    def generate(req: GenerateRequest):
        try:
            return service.generate_draft(req.target_type, req.sources, req.hint)
        except LLMUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))

    return app


def main() -> None:  # pragma: no cover - 구동 진입점
    """`python -m hub.interfaces.http_api` 로 HTTP 서버 구동."""
    import uvicorn

    config = Config()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    service = KnowledgeService(root, config)
    app = build_app(service)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":  # pragma: no cover
    main()
