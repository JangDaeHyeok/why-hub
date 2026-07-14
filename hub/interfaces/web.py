"""읽기 UI — FastAPI + HTMX 서버 렌더링 (M6, UI 스펙 §8).

기획안1 §9.1 읽기 기능: 목록·조회·검색·이력·관계. **UI 는 service 코어만 경유**하고
파일/DB 에 직접 접근하지 않는다(§9.3). JSON API(build_app) 위에 UI 라우트·정적 파일을 얹는다.

- 마크다운은 서버에서 렌더(markdown-it-py, raw HTML 비활성).
- 검색은 HTMX 부분 스왑(HX-Request 시 조각, 아니면 전체 페이지) — JS 없이도 폼 제출로 동작(점진 향상).
- 테마(라이트/다크)는 정적 CSS 디자인 토큰 + <head> 인라인 스크립트로 처리.

구현 Phase: M6.
"""

from __future__ import annotations

import os
from dataclasses import is_dataclass
from pathlib import Path

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from ..config import Config
from ..llm import LLMUnavailable
from ..service import KnowledgeService
from ..store.lint import LintError
from ..store.locking import LockTimeout
from .http_api import build_app

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
_TEMPLATES_DIR = _WEB_DIR.parent / "templates"  # ADR/DI 스캐폴드 템플릿


def build_web_app(service: KnowledgeService):
    """JSON API + 읽기 UI 를 함께 제공하는 앱."""
    app = build_app(service)  # JSON API 라우트·에러 핸들러 재사용
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
    md = MarkdownIt("commonmark", {"html": False})
    # 템플릿 전역: 프로젝트 셀렉터·배지가 매 페이지에서 쓴다(현재 프로젝트는 request.cookies 로 읽음).
    templates.env.globals["default_project"] = service.config.default_project
    templates.env.globals["all_projects"] = service.list_projects

    _ALL = "__all__"  # 쿠키 sentinel — 전체 프로젝트(필터 생략)

    def _current_project(request: Request) -> str | None:
        """현재 프로젝트(스코프용). 쿠키 없으면 기본 프로젝트, sentinel 이면 None(전체)."""
        p = request.cookies.get("__project")
        if p == _ALL:
            return None
        return p or service.config.default_project

    def _doc_or_404(doc_id: str) -> dict:
        doc = service.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return doc

    @app.post("/ui/project")
    def ui_set_project(request: Request, project: str = Form("")):
        """상단 셀렉터 — 현재 프로젝트를 쿠키에 저장하고 이전 페이지로."""
        val = project.strip() or service.config.default_project
        resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        resp.set_cookie("__project", val, max_age=31536000, path="/")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def ui_home(request: Request):
        docs = service.list_documents(project=_current_project(request))
        return templates.TemplateResponse(
            request, "list.html", {"docs": docs, "hits": None, "q": None}
        )

    @app.get("/ui/search", response_class=HTMLResponse)
    def ui_search(request: Request, q: str = "", type: str | None = None):
        project = _current_project(request)
        filters = {"type": type} if type else None
        if q.strip():
            hits = service.search_knowledge(q, filters, k=20, project=project)
            docs = None
        else:
            hits = None
            docs = service.list_documents(filters, project=project)
        ctx = {"hits": hits, "docs": docs, "q": q}
        # HTMX 부분 스왑이면 결과 조각만, 아니면 전체 페이지.
        template = "_results.html" if request.headers.get("hx-request") else "list.html"
        return templates.TemplateResponse(request, template, ctx)

    @app.get("/ui/docs/{doc_id}", response_class=HTMLResponse)
    def ui_document(request: Request, doc_id: str):
        doc = _doc_or_404(doc_id)
        return templates.TemplateResponse(
            request, "document.html",
            {"doc": doc, "body_html": md.render(doc["body"]), "active": "body"},
        )

    @app.get("/ui/docs/{doc_id}/history", response_class=HTMLResponse)
    def ui_history(request: Request, doc_id: str):
        doc = _doc_or_404(doc_id)
        entries = service.get_history(doc_id)
        return templates.TemplateResponse(
            request, "history.html",
            {"doc": doc, "entries": entries, "active": "history"},
        )

    @app.get("/ui/docs/{doc_id}/related", response_class=HTMLResponse)
    def ui_related(request: Request, doc_id: str):
        doc = _doc_or_404(doc_id)
        rel = service.get_related(doc_id)
        return templates.TemplateResponse(
            request, "related.html", {"doc": doc, "rel": rel, "active": "related"},
        )

    # ── 쓰기 (직접 작성/편집 — save 루틴 경유, lint 피드백) ───────────
    def _edit_ctx(markdown, actor, intended_diff, title, errors=None):
        return {
            "markdown": markdown, "actor": actor, "intended_diff": intended_diff,
            "title": title, "action": "/ui/save", "errors": errors,
        }

    @app.get("/ui/new", response_class=HTMLResponse)
    def ui_new(request: Request, template: str | None = None):
        md_text = ""
        if template in ("adr", "design-intent"):
            tp = _TEMPLATES_DIR / f"{template}.md"
            if tp.exists():
                md_text = tp.read_text(encoding="utf-8")
        return templates.TemplateResponse(
            request, "edit.html", _edit_ctx(md_text, "anonymous", "", "새 문서")
        )

    @app.get("/ui/docs/{doc_id}/edit", response_class=HTMLResponse)
    def ui_edit(request: Request, doc_id: str):
        raw = service.get_raw(doc_id)
        if raw is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return templates.TemplateResponse(
            request, "edit.html", _edit_ctx(raw, "anonymous", "", f"편집 · {doc_id}")
        )

    @app.post("/ui/save")
    def ui_save(
        request: Request,
        markdown: str = Form(...),
        actor: str = Form("anonymous"),
        intended_diff: str = Form(""),
    ):
        try:
            res = service.save_document(
                markdown, actor=actor, intended_diff=(intended_diff or None),
                project=_current_project(request),
            )
        except LintError as e:
            # lint 실패 → 저장 차단, 입력 보존 + 사유 배너 재표시 (422).
            return templates.TemplateResponse(
                request, "edit.html",
                _edit_ctx(markdown, actor, intended_diff, "문서 편집", errors=e.reasons),
                status_code=422,
            )
        except LockTimeout as e:
            return templates.TemplateResponse(
                request, "edit.html",
                _edit_ctx(markdown, actor, intended_diff, "문서 편집",
                          errors=[f"문서 락 타임아웃: {e}"]),
                status_code=409,
            )
        # 승인 게이트 on → 제출 dict(승인 대기). off → SaveResult(즉시 반영).
        if is_dataclass(res):
            return RedirectResponse(url=f"/ui/docs/{res.id}", status_code=303)
        return templates.TemplateResponse(request, "submitted.html", res)

    # ── AI 생성 (경로 B) — 초안 생성 → 편집 화면으로 (저장은 사람 검토) ─
    @app.get("/ui/generate", response_class=HTMLResponse)
    def ui_generate_form(request: Request):
        return templates.TemplateResponse(
            request, "generate.html",
            {"hint": "", "source_ids": "", "source_text": "", "error": None},
        )

    @app.post("/ui/generate")
    def ui_generate(
        request: Request,
        target_type: str = Form("adr"),
        hint: str = Form(""),
        source_ids: str = Form(""),
        source_text: str = Form(""),
    ):
        sources: list[dict] = []
        for sid in (s.strip() for s in source_ids.split(",")):
            if sid:
                sources.append({"kind": "doc", "id": sid})
        if source_text.strip():
            sources.append({"kind": "note", "text": source_text})
        try:
            result = service.generate_draft(
                target_type, sources, hint or None, project=_current_project(request)
            )
        except LLMUnavailable as e:
            # LLM 미구성 → 생성 폼에 안내(직접 작성은 항상 가능).
            return templates.TemplateResponse(
                request, "generate.html",
                {"hint": hint, "source_ids": source_ids, "source_text": source_text,
                 "error": f"{e} — '새 문서'로 직접 작성할 수 있습니다."},
                status_code=503,
            )
        # 초안을 편집 화면으로 — 사람 검토·수정 후 /ui/save 로 저장.
        lint = result["lint"]
        return templates.TemplateResponse(
            request, "edit.html",
            {
                "markdown": result["draft_markdown"],
                "actor": "anonymous",
                "intended_diff": "",
                "title": "AI 초안 검토",
                "action": "/ui/save",
                "errors": None,
                "warnings": None if lint["ok"] else lint["reasons"],
            },
        )

    # ── 멀티턴 AI 채팅 (스트리밍은 /chat/stream·/chat/apply JSON 을 chat.js 가 소비) ──
    @app.get("/ui/chat", response_class=HTMLResponse)
    def ui_chat(request: Request):
        # 현재 프로젝트를 chat.js 로 전달(대화가 그 프로젝트로 스코프됨).
        return templates.TemplateResponse(
            request, "chat.html", {"chat_project": _current_project(request) or ""}
        )

    # ── 승인함 (관리자 승인/반려 — 서버 렌더 폼) ──────────────────────
    def _approvals_ctx(request, reviewer="", message=None, error=None):
        # 현재 프로젝트의 대기 제출만(전체 모드면 None → 전부).
        return {
            "subs": service.list_submissions("pending", project=_current_project(request)),
            "reviewer": reviewer, "message": message, "error": error,
        }

    @app.get("/ui/approvals", response_class=HTMLResponse)
    def ui_approvals(request: Request):
        return templates.TemplateResponse(request, "approvals.html", _approvals_ctx(request))

    @app.post("/ui/approvals/{sub_id}/approve", response_class=HTMLResponse)
    def ui_approve(request: Request, sub_id: str, approver: str = Form("")):
        message = error = None
        try:
            res = service.approve_submission(sub_id, approver=approver)
            message = f"승인 완료 — {res.id} 반영됨 (change_type={res.change_type})."
        except PermissionError as e:
            error = f"승인 권한 없음: {e} (config admins 확인)"
        except LintError as e:
            error = "정식 lint 실패로 반영되지 않음(제출 대기 유지): " + "; ".join(e.reasons)
        except (KeyError, ValueError) as e:
            error = str(e)
        return templates.TemplateResponse(
            request, "approvals.html",
            _approvals_ctx(request, reviewer=approver, message=message, error=error),
        )

    @app.post("/ui/approvals/{sub_id}/reject", response_class=HTMLResponse)
    def ui_reject(request: Request, sub_id: str, approver: str = Form(""), note: str = Form("")):
        message = error = None
        try:
            service.reject_submission(sub_id, approver=approver, note=note)
            message = f"반려됨 — {sub_id}."
        except PermissionError as e:
            error = f"반려 권한 없음: {e} (config admins 확인)"
        except (KeyError, ValueError) as e:
            error = str(e)
        return templates.TemplateResponse(
            request, "approvals.html",
            _approvals_ctx(request, reviewer=approver, message=message, error=error),
        )

    return app


def main() -> None:  # pragma: no cover - 구동 진입점
    import uvicorn

    config = Config.load_default()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    service = KnowledgeService(root, config)
    host = os.environ.get("HOST", "127.0.0.1")  # 배포(컨테이너)는 0.0.0.0
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_web_app(service), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
