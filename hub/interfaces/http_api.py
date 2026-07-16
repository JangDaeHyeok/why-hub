"""HTTP API — JSON 엔드포인트 (FastAPI) + 인증/인가 (구현스펙-인증인가-RBAC.md).

각 엔드포인트는 **service 호출만** 한다(로직 중복 없음). 인증은 웹 세션 쿠키(브라우저) 기준이고,
인가는 공유 policy(`require_scope`)로 강제한다. actor 는 요청 바디가 아니라 **인증된 세션**에서
가져온다(사용자 입력 actor/approver 신뢰 제거). PAT→JWT 교환·JWKS·health 만 무인증(공개) 경로.

에러: 인증 실패→401, scope 부족→403(PermissionError 핸들러), LintError→422, LockTimeout→409, CSRF 실패→403.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..auth.deps import LOCAL_PRINCIPAL, bearer_token, csrf_ok, resolve_session
from ..auth.principal import SCOPE_READ, SCOPE_REVIEW, SCOPE_SUBMIT, require_scope
from ..auth.service import AuthError, RateLimited
from ..config import Config
from ..llm import LLMUnavailable
from ..service import KnowledgeService
from ..store.lint import LintError
from ..store.locking import LockTimeout
from ..store.normalize import normalize


class SaveRequest(BaseModel):
    """PUT /docs/{id} 요청 바디. actor 는 인증 세션에서 채운다(바디로 받지 않음).

    change_type 은 받지 않는다 — 저장 엔진이 스냅샷·상태 전이로 자동 판정한다(클라이언트가 이력
    타입을 위조하지 못하게). 폐기/대체 등은 frontmatter status/supersedes 변경으로 자동 감지된다."""

    markdown: str
    intended_diff: str | None = None
    project: str | None = None


class IngestRequest(BaseModel):
    """POST /ingest 요청 바디. source_ref 로 멱등 갱신."""

    source_ref: str
    content: str
    doc_type: str = "reference"
    title: str | None = None
    project: str | None = None


class GenerateRequest(BaseModel):
    """POST /generate 요청 바디 (AI 초안 — 저장 안 함)."""

    target_type: str = "adr"
    sources: list[dict] = []
    hint: str | None = None
    project: str | None = None


class ReviewRequest(BaseModel):
    """POST /submissions/{id}/approve|reject 요청 바디. approver 는 인증 세션에서 결정한다."""

    note: str = ""


class ChatRequest(BaseModel):
    """POST /chat, /chat/stream 요청 바디 (멀티턴 AI 생성)."""

    session_id: str | None = None
    message: str
    target_type: str = "adr"
    project: str | None = None


class ChatApplyRequest(BaseModel):
    """POST /chat/apply — staged 변경을 승인 큐에 제출."""

    session_id: str


def _ser(res):
    """SaveResult(데이터클래스) 또는 제출 dict 를 JSON 직렬화 가능 형태로."""
    return asdict(res) if is_dataclass(res) else res


def _client_key(request: Request) -> str:
    return request.client.host if request.client else ""


def build_app(service: KnowledgeService, auth=None) -> FastAPI:
    """JSON API. auth(AuthService)가 주어지면 세션 인증·인가·CSRF 를 강제, None 이면 로컬(무인증) 모드.

    무인증 모드는 로컬 개발/단위 테스트용(AUTH_ENABLED=false) — 전권 로컬 주체를 쓴다.
    """
    app = FastAPI(title="knowledge-hub", version="0.1.0", docs_url=None, redoc_url=None)
    cookie_name = auth.config.cookie_name if auth is not None else "wh_session"

    # ── 에러 매핑 ──────────────────────────────────────────────────────
    @app.exception_handler(LintError)
    async def _lint_error(request, exc: LintError):
        return JSONResponse(status_code=422, content={"error": "lint", "reasons": exc.reasons})

    @app.exception_handler(LockTimeout)
    async def _lock_timeout(request, exc: LockTimeout):
        return JSONResponse(status_code=409, content={"error": "lock_timeout", "detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def _permission(request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"error": "forbidden", "detail": str(exc)})

    # ── 인증 헬퍼 ──────────────────────────────────────────────────────
    def _principal(request: Request, *, need: str | None = None, csrf: bool = False):
        """세션→Principal. 미인증 401, scope 부족은 PermissionError(→403). CSRF 는 상태변경에 강제."""
        session, principal = resolve_session(auth, request, cookie_name)
        if principal is None:
            raise HTTPException(status_code=401, detail="인증이 필요합니다.")
        if csrf and auth is not None:
            if not csrf_ok(session, request.headers.get("x-csrf-token")):
                raise HTTPException(status_code=403, detail="CSRF 검증 실패")
        if need is not None:
            require_scope(principal, need)
        return principal

    def _require(doc_id: str, principal=None) -> dict:
        # 접근 불가 문서는 미존재와 동일하게 404(존재 노출 방지).
        doc = service.get_document(doc_id, principal=principal)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return doc

    # ── 공개 경로: health · JWKS · PAT→JWT 교환 ────────────────────────
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/.well-known/jwks.json")
    def jwks():
        if auth is None or auth.issuer is None:
            raise HTTPException(status_code=404, detail="JWKS 미구성")
        return JSONResponse(content=auth.issuer.jwks())

    @app.post("/api/auth/token/exchange")
    def token_exchange(request: Request):
        """Authorization: Bearer <PAT> → 단기 JWT. 응답 no-store, rate-limit."""
        if auth is None:
            raise HTTPException(status_code=404, detail="인증 비활성")
        pat = bearer_token(request)
        if not pat:
            raise HTTPException(status_code=401, detail="PAT Bearer 토큰이 필요합니다.")
        try:
            body = auth.exchange_pat_for_jwt(pat, client_key=_client_key(request))
        except RateLimited as e:
            raise HTTPException(status_code=429, detail=str(e))
        except AuthError as e:
            raise HTTPException(status_code=401, detail=str(e))
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    # ── 검색/읽기 (READ) ──────────────────────────────────────────────
    @app.get("/search")
    def search(
        request: Request,
        q: str,
        type: str | None = None,
        status: str | None = None,
        project: str | None = None,
        tags: list[str] | None = Query(None),
        k: int = 10,
    ):
        principal = _principal(request, need=SCOPE_READ)
        filters = {
            key: val
            for key, val in (("type", type), ("status", status),
                             ("project", project), ("tags", tags))
            if val is not None
        }
        return service.search_knowledge(q, filters, k, principal=principal)

    @app.get("/projects")
    def list_projects(request: Request):
        principal = _principal(request, need=SCOPE_READ)
        return service.list_projects(principal=principal)

    @app.get("/docs")
    def list_docs(
        request: Request,
        type: str | None = None,
        status: str | None = None,
        project: str | None = None,
        tags: list[str] | None = Query(None),
        limit: int | None = None,
        offset: int = 0,
    ):
        principal = _principal(request, need=SCOPE_READ)
        filters = {
            key: val
            for key, val in (("type", type), ("status", status),
                             ("project", project), ("tags", tags))
            if val is not None
        }
        return service.list_documents(filters, limit=limit, offset=offset, principal=principal)

    @app.get("/docs/{doc_id}")
    def get_doc(request: Request, doc_id: str):
        principal = _principal(request, need=SCOPE_READ)
        return _require(doc_id, principal)

    # ── 저장 (SUBMIT + CSRF) ──────────────────────────────────────────
    @app.put("/docs/{doc_id}")
    def put_doc(request: Request, doc_id: str, req: SaveRequest):
        principal = _principal(request, need=SCOPE_SUBMIT, csrf=True)
        try:
            nd = normalize(req.markdown)
        except yaml.YAMLError as e:
            raise LintError([f"frontmatter YAML 파싱 실패: {e}"]) from e
        if nd.id and nd.id != doc_id:
            raise HTTPException(
                status_code=422, detail=f"id 불일치: 경로 {doc_id} ≠ 문서 {nd.id}"
            )
        res = service.save_document(
            req.markdown,
            actor=principal.username,
            intended_diff=req.intended_diff,
            project=req.project,
            principal=principal,
        )
        return _ser(res)

    @app.get("/docs/{doc_id}/history")
    def get_history(request: Request, doc_id: str, anchor: str | None = None, limit: int | None = None):
        principal = _principal(request, need=SCOPE_READ)
        _require(doc_id, principal)
        return service.get_history(doc_id, anchor=anchor, limit=limit, principal=principal)

    @app.get("/docs/{doc_id}/diff")
    def get_diff(request: Request, doc_id: str, date: str | None = None):
        principal = _principal(request, need=SCOPE_READ)
        _require(doc_id, principal)
        return service.get_docs_diff(doc_id, date=date, principal=principal)

    @app.get("/docs/{doc_id}/related")
    def get_related(request: Request, doc_id: str):
        principal = _principal(request, need=SCOPE_READ)
        rel = service.get_related(doc_id, principal=principal)
        if rel is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return rel

    # ── 인제스천 (SUBMIT + CSRF) ──────────────────────────────────────
    @app.post("/ingest")
    def ingest(request: Request, req: IngestRequest):
        principal = _principal(request, need=SCOPE_SUBMIT, csrf=True)
        res = service.ingest_source(
            req.source_ref,
            content=req.content,
            actor=principal.username,
            doc_type=req.doc_type,
            title=req.title,
            project=req.project,
            principal=principal,
        )
        return _ser(res)

    # ── 승인 워크플로우 ────────────────────────────────────────────────
    @app.get("/submissions")
    def list_submissions(request: Request, status: str | None = None, project: str | None = None):
        principal = _principal(request, need=SCOPE_SUBMIT)
        subs = service.list_submissions(status, project=project)
        # admin(REVIEW)=전체, member=본인 제출만 (인터페이스 인가 — §2.1).
        if not principal.has_scope(SCOPE_REVIEW):
            subs = [s for s in subs if s.get("actor") == principal.username]
        return subs

    @app.get("/submissions/{sub_id}")
    def get_submission(request: Request, sub_id: str):
        principal = _principal(request, need=SCOPE_SUBMIT)
        sub = service.get_submission(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail=f"제출 없음: {sub_id}")
        if not principal.has_scope(SCOPE_REVIEW) and sub.get("actor") != principal.username:
            raise HTTPException(status_code=403, detail="자신의 제출만 조회할 수 있습니다.")
        return sub

    @app.post("/submissions/{sub_id}/approve")
    def approve_submission(request: Request, sub_id: str, req: ReviewRequest):
        principal = _principal(request, need=SCOPE_REVIEW, csrf=True)
        try:
            res = service.approve_submission(sub_id, principal=principal)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return asdict(res)

    @app.post("/submissions/{sub_id}/reject")
    def reject_submission(request: Request, sub_id: str, req: ReviewRequest):
        principal = _principal(request, need=SCOPE_REVIEW, csrf=True)
        try:
            return service.reject_submission(sub_id, principal=principal, note=req.note)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    # ── AI 생성 (READ — 초안 반환만) ──────────────────────────────────
    @app.post("/generate")
    def generate(request: Request, req: GenerateRequest):
        principal = _principal(request, need=SCOPE_SUBMIT, csrf=True)
        try:
            return service.generate_draft(
                req.target_type, req.sources, req.hint, project=req.project,
                principal=principal,
            )
        except LLMUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))

    @app.post("/chat")
    def chat(request: Request, req: ChatRequest):
        principal = _principal(request, need=SCOPE_SUBMIT, csrf=True)
        try:
            return service.chat_turn(
                req.session_id, req.message, actor=principal.username,
                target_type=req.target_type, project=req.project, principal=principal,
            )
        except LLMUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))

    @app.post("/chat/stream")
    def chat_stream(request: Request, req: ChatRequest):
        principal = _principal(request, need=SCOPE_SUBMIT, csrf=True)
        if not service.llm_available:
            raise HTTPException(status_code=503, detail="LLM 미구성 — 멀티턴 채팅 비활성")

        def sse():
            gen = service.chat_turn_stream(
                req.session_id, req.message, actor=principal.username,
                target_type=req.target_type, project=req.project, principal=principal,
            )
            for event in gen:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    @app.post("/chat/apply")
    def chat_apply(request: Request, req: ChatApplyRequest):
        principal = _principal(request, need=SCOPE_SUBMIT, csrf=True)
        try:
            return service.apply_session(
                req.session_id, actor=principal.username, principal=principal
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    return app


def main() -> None:  # pragma: no cover - 구동 진입점
    """`python -m hub.interfaces.http_api` 로 HTTP 서버 구동."""
    import uvicorn

    from ..auth.service import build_auth_service

    config = Config.load_default()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    service = KnowledgeService(root, config)
    auth = build_auth_service(config, root) if config.auth.enabled else None
    app = build_app(service, auth)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
