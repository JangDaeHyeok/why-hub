"""HTTP API — 읽기 JSON 엔드포인트 (FastAPI) ([기획안1 §11] HTTP 열).

UI(M6~) 가 소비할 읽기 경로. 각 엔드포인트는 **service 호출만** 한다(로직 중복 없음).
에러 매핑: LintError→422, not found→404, LockTimeout→409.
CORS/정적 서빙/HTMX 는 UI Phase(M6~) — 지금은 순수 JSON.

구현 Phase: P08.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
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
    project: str | None = None


class IngestRequest(BaseModel):
    """POST /ingest 요청 바디. source_ref 로 멱등 갱신."""

    source_ref: str
    content: str
    doc_type: str = "reference"
    title: str | None = None
    actor: str = "ingest"
    project: str | None = None


class GenerateRequest(BaseModel):
    """POST /generate 요청 바디 (AI 초안 — 저장 안 함)."""

    target_type: str = "adr"
    sources: list[dict] = []
    hint: str | None = None
    project: str | None = None


class ReviewRequest(BaseModel):
    """POST /submissions/{id}/approve|reject 요청 바디. approver 는 관리자여야 한다."""

    approver: str
    note: str = ""


class ChatRequest(BaseModel):
    """POST /chat, /chat/stream 요청 바디 (멀티턴 AI 생성)."""

    session_id: str | None = None
    message: str
    actor: str = "anonymous"
    target_type: str = "adr"
    project: str | None = None


class ChatApplyRequest(BaseModel):
    """POST /chat/apply — staged 변경을 승인 큐에 제출."""

    session_id: str
    actor: str = "anonymous"


def _ser(res):
    """SaveResult(데이터클래스) 또는 제출 dict 를 JSON 직렬화 가능 형태로."""
    return asdict(res) if is_dataclass(res) else res


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

    @app.exception_handler(PermissionError)
    async def _permission(request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"error": "forbidden", "detail": str(exc)})

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
        project: str | None = None,
        tags: list[str] | None = Query(None),
        k: int = 10,
    ):
        filters = {
            key: val
            for key, val in (("type", type), ("status", status),
                             ("project", project), ("tags", tags))
            if val is not None
        }
        return service.search_knowledge(q, filters, k)

    @app.get("/projects")
    def list_projects():
        """인덱스에 존재하는 project 목록."""
        return service.list_projects()

    # ── 목록 ──────────────────────────────────────────────────────────
    @app.get("/docs")
    def list_docs(
        type: str | None = None,
        status: str | None = None,
        project: str | None = None,
        tags: list[str] | None = Query(None),
        limit: int | None = None,
        offset: int = 0,
    ):
        filters = {
            key: val
            for key, val in (("type", type), ("status", status),
                             ("project", project), ("tags", tags))
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
        # 경로 id 와 문서 frontmatter id 일치 검증. 깨진 YAML 은 LintError(422)로 매핑
        # (save_document 의 게이트와 동일 경로 — 여기서 raw YAML 예외로 500 나는 것 방지).
        try:
            nd = normalize(req.markdown)
        except yaml.YAMLError as e:
            raise LintError([f"frontmatter YAML 파싱 실패: {e}"]) from e
        if nd.id and nd.id != doc_id:
            raise HTTPException(
                status_code=422, detail=f"id 불일치: 경로 {doc_id} ≠ 문서 {nd.id}"
            )
        # LintError → 422(핸들러), LockTimeout → 409(핸들러).
        # 승인 게이트 on 이면 즉시 반영 대신 제출 dict 반환(승인 대기).
        res = service.save_document(
            req.markdown,
            actor=req.actor,
            change_type=req.change_type,
            intended_diff=req.intended_diff,
            project=req.project,
        )
        return _ser(res)

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
            project=req.project,
        )
        return _ser(res)

    # ── 승인 워크플로우 (구현스펙-승인워크플로우.md) ────────────────────
    @app.get("/submissions")
    def list_submissions(status: str | None = None, project: str | None = None):
        return service.list_submissions(status, project=project)

    @app.get("/submissions/{sub_id}")
    def get_submission(sub_id: str):
        sub = service.get_submission(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail=f"제출 없음: {sub_id}")
        return sub

    @app.post("/submissions/{sub_id}/approve")
    def approve_submission(sub_id: str, req: ReviewRequest):
        # PermissionError→403(핸들러), LintError→422, LockTimeout→409.
        try:
            res = service.approve_submission(sub_id, approver=req.approver)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return asdict(res)

    @app.post("/submissions/{sub_id}/reject")
    def reject_submission(sub_id: str, req: ReviewRequest):
        try:
            return service.reject_submission(sub_id, approver=req.approver, note=req.note)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    # ── AI 생성 (초안 반환만, 저장 안 함) ─────────────────────────────
    @app.post("/generate")
    def generate(req: GenerateRequest):
        try:
            return service.generate_draft(
                req.target_type, req.sources, req.hint, project=req.project
            )
        except LLMUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))

    # ── 멀티턴 AI 생성 (펑션콜 + 최종 응답 스트리밍) ──────────────────
    @app.post("/chat")
    def chat(req: ChatRequest):
        """비스트리밍 1턴 → {session_id, reply, staged}."""
        try:
            return service.chat_turn(
                req.session_id, req.message, actor=req.actor,
                target_type=req.target_type, project=req.project,
            )
        except LLMUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))

    @app.post("/chat/stream")
    def chat_stream(req: ChatRequest):
        """스트리밍 1턴 (SSE). 도구 해결(stream=false) 후 최종 답변 토큰(stream=true)."""
        if not service.llm_available:
            raise HTTPException(status_code=503, detail="LLM 미구성 — 멀티턴 채팅 비활성")

        def sse():
            gen = service.chat_turn_stream(
                req.session_id, req.message, actor=req.actor,
                target_type=req.target_type, project=req.project,
            )
            for event in gen:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    @app.post("/chat/apply")
    def chat_apply(req: ChatApplyRequest):
        """세션의 staged 변경을 승인 큐에 제출 → 제출 목록."""
        try:
            return service.apply_session(req.session_id, actor=req.actor)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    return app


def main() -> None:  # pragma: no cover - 구동 진입점
    """`python -m hub.interfaces.http_api` 로 HTTP 서버 구동."""
    import uvicorn

    config = Config.load_default()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    service = KnowledgeService(root, config)
    app = build_app(service)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
