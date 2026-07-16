"""읽기·쓰기 UI + 인증 UI — FastAPI + HTMX 서버 렌더링 (구현스펙-인증인가-RBAC.md).

JSON API(build_app) 위에 UI 라우트·정적 파일·인증 페이지를 얹는다. **UI 는 service 코어만 경유**한다.
인증은 opaque 세션 쿠키(HttpOnly·SameSite=Lax·Secure), 인가는 공유 policy(scope). 미로그인 시 보호
페이지는 로그인으로 redirect(오류는 API=401/403, UI=redirect 로 구분). actor/approver 입력란은 제거하고
값은 인증 세션에서 가져온다. 모든 쿠키 인증 상태변경 폼에 CSRF 히든필드를 검증한다.
"""

from __future__ import annotations

import os
from dataclasses import is_dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from ..auth.deps import csrf_ok, resolve_session
from ..auth.principal import SCOPE_READ, SCOPE_REVIEW, SCOPE_SUBMIT
from ..auth.service import AuthError, RateLimited
from ..config import Config
from ..llm import LLMUnavailable
from ..service import KnowledgeService
from ..store.lint import LintError
from ..store.locking import LockTimeout
from .http_api import build_app

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
_TEMPLATES_DIR = _WEB_DIR.parent / "templates"  # ADR/DI 스캐폴드 템플릿


class _LoginRequired(Exception):
    """보호 UI 접근 시 미로그인 — 로그인 페이지로 redirect."""

    def __init__(self, next_url: str):
        self.next_url = next_url


def _safe_next(raw: str | None) -> str:
    """오픈 redirect 방지 — 자기 경로(/로 시작, //·백슬래시 아님)만 허용."""
    if raw and raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return raw
    return "/"


def build_web_app(service: KnowledgeService, auth=None):
    """JSON API + 읽기/쓰기 UI + 인증 UI. auth(AuthService)=None 이면 로컬(무인증) 모드."""
    app = build_app(service, auth)  # JSON API 라우트·에러 핸들러 재사용
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
    md = MarkdownIt("commonmark", {"html": False})
    templates.env.globals["default_project"] = service.config.default_project

    cookie_name = auth.config.cookie_name if auth is not None else "wh_session"
    auth_enabled = auth is not None
    cookie_secure = auth.config.cookie_secure if auth is not None else False
    signup_enabled = auth.config.signup_enabled if auth is not None else True
    session_max_age = auth.config.session_ttl_seconds if auth is not None else 0

    _ALL = "__all__"

    # ── 인증 헬퍼 ──────────────────────────────────────────────────────
    def _auth_ctx(request: Request):
        """(session, principal). 미인증(auth 활성)이면 (None, None), 무인증 모드면 (None, LOCAL)."""
        return resolve_session(auth, request, cookie_name)

    def _require_login(request: Request, *, need: str = SCOPE_READ):
        session, principal = _auth_ctx(request)
        if principal is None:
            raise _LoginRequired(request.url.path)
        if need and not principal.has_scope(need):
            raise HTTPException(status_code=403, detail="권한이 없습니다.")
        return session, principal

    def _check_csrf(session, token: str) -> None:
        if auth is not None and not csrf_ok(session, token):
            raise HTTPException(status_code=403, detail="CSRF 검증 실패")

    def _check_origin(request: Request) -> None:
        """세션-전(로그인·가입) 상태변경의 login-CSRF 방어 — 교차 출처 POST 차단.

        세션 CSRF 토큰이 아직 없는 단계라 _check_csrf 가 걸리지 않는다. 브라우저는 교차 출처
        POST 에 항상 Origin(없으면 Referer)을 붙이므로, 그 host 가 요청 host 와 다르면 거부한다.
        헤더가 아예 없으면(비브라우저 클라이언트·동일 출처 일부) 통과 — 무인증 모드는 no-op."""
        if not auth_enabled:
            return
        from urllib.parse import urlsplit

        host = request.headers.get("host")
        src = request.headers.get("origin") or request.headers.get("referer")
        if not src or not host:
            return
        if urlsplit(src).netloc != host:
            raise HTTPException(status_code=403, detail="출처 검증 실패(CSRF)")

    def _nav(session, principal) -> dict:
        """base.html 이 쓰는 공통 컨텍스트(사용자·역할·CSRF·auth 활성·접근가능 프로젝트)."""
        return {
            "principal": principal,
            "auth_enabled": auth_enabled,
            "signup_enabled": signup_enabled,
            "csrf_token": (session or {}).get("csrf_token", "") if session else "",
            # 셀렉터는 접근 가능한 프로젝트만 노출(admin=전체).
            "all_projects": service.list_projects(principal=principal) if principal else [],
        }

    def _render(request, template, session, principal, ctx: dict, **kw):
        return templates.TemplateResponse(
            request, template, {**_nav(session, principal), **ctx}, **kw
        )

    def _set_session_cookie(resp, token: str) -> None:
        resp.set_cookie(
            cookie_name, token, max_age=session_max_age or None, path="/",
            httponly=True, samesite="lax", secure=cookie_secure,
        )

    @app.exception_handler(_LoginRequired)
    async def _login_redirect(request, exc: _LoginRequired):
        return RedirectResponse(f"/ui/login?next={quote(exc.next_url)}", status_code=303)

    def _current_project(request: Request, principal=None) -> str | None:
        p = request.cookies.get("__project")
        if p == _ALL:
            return None
        val = p or service.config.default_project
        # 접근 불가한 프로젝트가 쿠키에 남아 있으면 기본 프로젝트로 폴백.
        if principal is not None and not principal.can_read(val, service.config.default_project):
            return service.config.default_project
        return val

    def _doc_or_404(doc_id: str, principal=None) -> dict:
        doc = service.get_document(doc_id, principal=principal)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return doc

    # ── 인증 페이지 (공개) ─────────────────────────────────────────────
    @app.get("/ui/login", response_class=HTMLResponse)
    def ui_login_form(request: Request, next: str = "/", error: str | None = None):
        return templates.TemplateResponse(
            request, "login.html",
            {**_nav(None, None), "next": _safe_next(next), "error": error},
        )

    @app.post("/ui/login")
    def ui_login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ):
        if auth is None:  # 무인증 모드 — 로그인 개념 없음
            return RedirectResponse("/", status_code=303)
        _check_origin(request)  # login-CSRF 방어(세션-전 상태변경)
        client_key = request.client.host if request.client else ""
        try:
            token, _session, _principal = auth.login(username, password, client_key=client_key)
        except (AuthError, RateLimited) as e:
            return templates.TemplateResponse(
                request, "login.html",
                {**_nav(None, None), "next": _safe_next(next), "error": str(e)},
                status_code=401,
            )
        resp = RedirectResponse(_safe_next(next), status_code=303)
        _set_session_cookie(resp, token)
        return resp

    @app.get("/ui/signup", response_class=HTMLResponse)
    def ui_signup_form(request: Request, error: str | None = None):
        if not signup_enabled:
            raise HTTPException(status_code=404, detail="회원가입 비활성")
        return templates.TemplateResponse(
            request, "signup.html", {**_nav(None, None), "error": error}
        )

    @app.post("/ui/signup")
    def ui_signup(request: Request, username: str = Form(...), password: str = Form(...)):
        if auth is None:
            return RedirectResponse("/", status_code=303)
        if not signup_enabled:
            raise HTTPException(status_code=404, detail="회원가입 비활성")
        _check_origin(request)  # login-CSRF 방어(세션-전 상태변경)
        from ..auth.passwords import PasswordPolicyError
        from ..auth.repository import UsernameTaken

        client_key = request.client.host if request.client else ""
        try:
            auth.signup(username, password, client_key=client_key)
            token, _s, _p = auth.login(username, password, client_key=client_key)
        except (UsernameTaken, PasswordPolicyError, AuthError) as e:
            return templates.TemplateResponse(
                request, "signup.html", {**_nav(None, None), "error": str(e)},
                status_code=400,
            )
        resp = RedirectResponse("/", status_code=303)
        _set_session_cookie(resp, token)
        return resp

    @app.post("/ui/logout")
    def ui_logout(request: Request, csrf_token: str = Form("")):
        session, principal = _auth_ctx(request)
        if auth is not None:
            _check_csrf(session, csrf_token)
            auth.logout(request.cookies.get(cookie_name))
        resp = RedirectResponse("/ui/login", status_code=303)
        resp.delete_cookie(cookie_name, path="/")
        return resp

    # ── 내 계정 ────────────────────────────────────────────────────────
    @app.get("/ui/account", response_class=HTMLResponse)
    def ui_account(request: Request, message: str | None = None, error: str | None = None):
        session, principal = _require_login(request)
        return _render(request, "account.html", session, principal,
                       {"message": message, "error": error})

    @app.post("/ui/account/password")
    def ui_change_password(
        request: Request,
        old_password: str = Form(...),
        new_password: str = Form(...),
        csrf_token: str = Form(""),
    ):
        session, principal = _require_login(request)
        _check_csrf(session, csrf_token)
        if auth is None:
            return RedirectResponse("/ui/account", status_code=303)
        from ..auth.passwords import PasswordPolicyError

        try:
            auth.change_password(
                principal.user_id, old_password, new_password,
                current_session_id=(session or {}).get("id"),
            )
        except (AuthError, PasswordPolicyError) as e:
            return _render(request, "account.html", session, principal,
                           {"error": str(e)}, status_code=400)
        return _render(request, "account.html", session, principal,
                       {"message": "비밀번호가 변경되었습니다. 다른 세션은 로그아웃되었습니다."})

    # ── PAT 관리 ───────────────────────────────────────────────────────
    @app.get("/ui/account/tokens", response_class=HTMLResponse)
    def ui_tokens(request: Request, error: str | None = None):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        pats = auth.list_pats(principal.user_id) if auth is not None else []
        return _render(request, "tokens.html", session, principal,
                       {"pats": pats, "error": error})

    @app.post("/ui/account/tokens")
    def ui_create_token(
        request: Request,
        name: str = Form("token"),
        scope_read: str = Form(""),
        scope_submit: str = Form(""),
        csrf_token: str = Form(""),
    ):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        _check_csrf(session, csrf_token)
        if auth is None:
            return RedirectResponse("/ui/account/tokens", status_code=303)
        scopes = []
        if scope_read:
            scopes.append(SCOPE_READ)
        if scope_submit:
            scopes.append(SCOPE_SUBMIT)
        user = auth.repo.get_user_by_id(principal.user_id)
        try:
            full, pat = auth.create_pat(user, name=name, scopes=scopes)
        except PermissionError as e:
            return _render(request, "tokens.html", session, principal,
                           {"pats": auth.list_pats(principal.user_id), "error": str(e)},
                           status_code=403)
        # 원문은 1회만 표시.
        return _render(request, "token_created.html", session, principal,
                       {"full_token": full, "pat": pat})

    @app.post("/ui/account/tokens/{pat_id}/revoke")
    def ui_revoke_token(request: Request, pat_id: str, csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        _check_csrf(session, csrf_token)
        if auth is not None:
            auth.revoke_pat(pat_id, principal.user_id)  # 자신의 PAT만(user_id 조건)
        return RedirectResponse("/ui/account/tokens", status_code=303)

    # ── 프로젝트 셀렉터 ────────────────────────────────────────────────
    @app.post("/ui/project")
    def ui_set_project(request: Request, project: str = Form(""), csrf_token: str = Form("")):
        session, principal = _require_login(request)
        _check_csrf(session, csrf_token)
        val = project.strip() or service.config.default_project
        resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        resp.set_cookie("__project", val, max_age=31536000, path="/")
        return resp

    # ── 읽기 UI ────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def ui_home(request: Request):
        session, principal = _require_login(request)
        docs = service.list_documents(project=_current_project(request, principal),
                                      principal=principal)
        return _render(request, "list.html", session, principal,
                       {"docs": docs, "hits": None, "q": None})

    @app.get("/ui/search", response_class=HTMLResponse)
    def ui_search(request: Request, q: str = "", type: str | None = None):
        session, principal = _require_login(request)
        project = _current_project(request, principal)
        filters = {"type": type} if type else None
        if q.strip():
            hits = service.search_knowledge(q, filters, k=20, project=project, principal=principal)
            docs = None
        else:
            hits = None
            docs = service.list_documents(filters, project=project, principal=principal)
        ctx = {"hits": hits, "docs": docs, "q": q}
        template = "_results.html" if request.headers.get("hx-request") else "list.html"
        return _render(request, template, session, principal, ctx)

    @app.get("/ui/docs/{doc_id}", response_class=HTMLResponse)
    def ui_document(request: Request, doc_id: str):
        session, principal = _require_login(request)
        doc = _doc_or_404(doc_id, principal)
        return _render(request, "document.html", session, principal,
                       {"doc": doc, "body_html": md.render(doc["body"]), "active": "body"})

    @app.get("/ui/docs/{doc_id}/history", response_class=HTMLResponse)
    def ui_history(request: Request, doc_id: str):
        session, principal = _require_login(request)
        doc = _doc_or_404(doc_id, principal)
        entries = service.get_history(doc_id, principal=principal)
        return _render(request, "history.html", session, principal,
                       {"doc": doc, "entries": entries, "active": "history"})

    @app.get("/ui/docs/{doc_id}/related", response_class=HTMLResponse)
    def ui_related(request: Request, doc_id: str):
        session, principal = _require_login(request)
        doc = _doc_or_404(doc_id, principal)
        rel = service.get_related(doc_id, principal=principal)
        return _render(request, "related.html", session, principal,
                       {"doc": doc, "rel": rel, "active": "related"})

    # ── 쓰기 UI (SUBMIT) — actor 는 세션에서 ───────────────────────────
    def _edit_ctx(markdown, intended_diff, title, errors=None, warnings=None):
        return {
            "markdown": markdown, "intended_diff": intended_diff, "title": title,
            "action": "/ui/save", "errors": errors, "warnings": warnings,
        }

    @app.get("/ui/new", response_class=HTMLResponse)
    def ui_new(request: Request, template: str | None = None):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        md_text = ""
        if template in ("adr", "design-intent"):
            tp = _TEMPLATES_DIR / f"{template}.md"
            if tp.exists():
                md_text = tp.read_text(encoding="utf-8")
        return _render(request, "edit.html", session, principal,
                       _edit_ctx(md_text, "", "새 문서"))

    @app.get("/ui/docs/{doc_id}/edit", response_class=HTMLResponse)
    def ui_edit(request: Request, doc_id: str):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        raw = service.get_raw(doc_id, principal=principal)
        if raw is None:
            raise HTTPException(status_code=404, detail=f"문서 없음: {doc_id}")
        return _render(request, "edit.html", session, principal,
                       _edit_ctx(raw, "", f"편집 · {doc_id}"))

    @app.post("/ui/save")
    def ui_save(
        request: Request,
        markdown: str = Form(...),
        intended_diff: str = Form(""),
        csrf_token: str = Form(""),
    ):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        _check_csrf(session, csrf_token)
        try:
            res = service.save_document(
                markdown, actor=principal.username, intended_diff=(intended_diff or None),
                project=_current_project(request, principal), principal=principal,
            )
        except LintError as e:
            return _render(request, "edit.html", session, principal,
                           _edit_ctx(markdown, intended_diff, "문서 편집", errors=e.reasons),
                           status_code=422)
        except LockTimeout as e:
            return _render(request, "edit.html", session, principal,
                           _edit_ctx(markdown, intended_diff, "문서 편집",
                                     errors=[f"문서 락 타임아웃: {e}"]), status_code=409)
        except PermissionError as e:
            return _render(request, "edit.html", session, principal,
                           _edit_ctx(markdown, intended_diff, "문서 편집",
                                     errors=[f"프로젝트 쓰기 권한 없음: {e}"]), status_code=403)
        if is_dataclass(res):
            return RedirectResponse(url=f"/ui/docs/{res.id}", status_code=303)
        return _render(request, "submitted.html", session, principal, res)

    # ── AI 생성 (READ — 초안) ──────────────────────────────────────────
    @app.get("/ui/generate", response_class=HTMLResponse)
    def ui_generate_form(request: Request):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        return _render(request, "generate.html", session, principal,
                       {"hint": "", "source_ids": "", "source_text": "", "error": None})

    @app.post("/ui/generate")
    def ui_generate(
        request: Request,
        target_type: str = Form("adr"),
        hint: str = Form(""),
        source_ids: str = Form(""),
        source_text: str = Form(""),
        csrf_token: str = Form(""),
    ):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        _check_csrf(session, csrf_token)
        sources: list[dict] = []
        for sid in (s.strip() for s in source_ids.split(",")):
            if sid:
                sources.append({"kind": "doc", "id": sid})
        if source_text.strip():
            sources.append({"kind": "note", "text": source_text})
        try:
            result = service.generate_draft(
                target_type, sources, hint or None,
                project=_current_project(request, principal), principal=principal,
            )
        except LLMUnavailable as e:
            return _render(request, "generate.html", session, principal,
                           {"hint": hint, "source_ids": source_ids, "source_text": source_text,
                            "error": f"{e} — '새 문서'로 직접 작성할 수 있습니다."},
                           status_code=503)
        lint = result["lint"]
        return _render(request, "edit.html", session, principal, {
            "markdown": result["draft_markdown"], "intended_diff": "",
            "title": "AI 초안 검토", "action": "/ui/save", "errors": None,
            "warnings": None if lint["ok"] else lint["reasons"],
        })

    # ── 멀티턴 AI 채팅 ─────────────────────────────────────────────────
    @app.get("/ui/chat", response_class=HTMLResponse)
    def ui_chat(request: Request):
        session, principal = _require_login(request, need=SCOPE_SUBMIT)
        return _render(request, "chat.html", session, principal,
                       {"chat_project": _current_project(request, principal) or ""})

    # ── 승인함 (REVIEW — admin) ────────────────────────────────────────
    def _approvals_ctx(request, principal, message=None, error=None):
        return {
            "subs": service.list_submissions(
                "pending", project=_current_project(request, principal)),
            "message": message, "error": error,
        }

    @app.get("/ui/approvals", response_class=HTMLResponse)
    def ui_approvals(request: Request):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        return _render(request, "approvals.html", session, principal,
                       _approvals_ctx(request, principal))

    @app.post("/ui/approvals/{sub_id}/approve", response_class=HTMLResponse)
    def ui_approve(request: Request, sub_id: str, csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        _check_csrf(session, csrf_token)
        message = error = None
        try:
            res = service.approve_submission(sub_id, principal=principal)
            message = f"승인 완료 — {res.id} 반영됨 (change_type={res.change_type})."
        except PermissionError as e:
            error = f"승인 권한 없음: {e}"
        except LintError as e:
            error = "정식 lint 실패로 반영되지 않음(제출 대기 유지): " + "; ".join(e.reasons)
        except (KeyError, ValueError) as e:
            error = str(e)
        return _render(request, "approvals.html", session, principal,
                       _approvals_ctx(request, principal, message=message, error=error))

    @app.post("/ui/approvals/{sub_id}/reject", response_class=HTMLResponse)
    def ui_reject(request: Request, sub_id: str, note: str = Form(""), csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        _check_csrf(session, csrf_token)
        message = error = None
        try:
            service.reject_submission(sub_id, principal=principal, note=note)
            message = f"반려됨 — {sub_id}."
        except PermissionError as e:
            error = f"반려 권한 없음: {e}"
        except (KeyError, ValueError) as e:
            error = str(e)
        return _render(request, "approvals.html", session, principal,
                       _approvals_ctx(request, principal, message=message, error=error))

    # ── 프로젝트 관리 (admin 전용 = review scope) ──────────────────────
    @app.get("/ui/projects", response_class=HTMLResponse)
    def ui_projects(request: Request, error: str | None = None):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        projects = auth.list_all_projects() if auth is not None else []
        return _render(request, "projects.html", session, principal,
                       {"projects": projects, "error": error})

    @app.post("/ui/projects")
    def ui_create_project(request: Request, slug: str = Form(...), name: str = Form(""),
                          description: str = Form(""), csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        _check_csrf(session, csrf_token)
        if auth is None:
            return RedirectResponse("/ui/projects", status_code=303)
        from ..auth.repository import ProjectExists

        try:
            p = auth.create_project(slug, name, description, created_by=principal.user_id)
        except (ValueError, ProjectExists) as e:
            projects = auth.list_all_projects()
            return _render(request, "projects.html", session, principal,
                           {"projects": projects, "error": str(e)}, status_code=400)
        return RedirectResponse(f"/ui/projects/{p['slug']}", status_code=303)

    def _project_detail_ctx(slug, message=None, error=None):
        return {
            "project": auth.get_project(slug),
            "members": auth.project_members(slug),
            "users": auth.list_users(),
            # 삭제 시 남게 될 문서 수(경고 표시용). 기본 프로젝트는 삭제 불가.
            "doc_count": len(service.list_documents(project=slug)),
            "is_default": slug == service.config.default_project,
            "message": message, "error": error,
        }

    @app.get("/ui/projects/{slug}", response_class=HTMLResponse)
    def ui_project_detail(request: Request, slug: str):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        if auth is None or auth.get_project(slug) is None:
            raise HTTPException(status_code=404, detail=f"프로젝트 없음: {slug}")
        return _render(request, "project_detail.html", session, principal,
                       _project_detail_ctx(slug))

    @app.post("/ui/projects/{slug}")
    def ui_update_project(request: Request, slug: str, name: str = Form(""),
                          description: str = Form(""), csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        _check_csrf(session, csrf_token)
        if auth is not None and auth.get_project(slug) is not None:
            auth.update_project(slug, name, description)
        return RedirectResponse(f"/ui/projects/{slug}", status_code=303)

    @app.post("/ui/projects/{slug}/members")
    def ui_add_member(request: Request, slug: str, user_id: str = Form(...),
                      role: str = Form("viewer"), csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        _check_csrf(session, csrf_token)
        message = error = None
        if auth is not None:
            try:
                auth.add_project_member(slug, user_id, role, actor_id=principal.user_id)
                message = "멤버가 추가/갱신되었습니다."
            except ValueError as e:
                error = str(e)
        return _render(request, "project_detail.html", session, principal,
                       _project_detail_ctx(slug, message=message, error=error))

    @app.post("/ui/projects/{slug}/members/{user_id}/remove")
    def ui_remove_member(request: Request, slug: str, user_id: str, csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        _check_csrf(session, csrf_token)
        if auth is not None:
            auth.remove_project_member(slug, user_id, actor_id=principal.user_id)
        return RedirectResponse(f"/ui/projects/{slug}", status_code=303)

    @app.post("/ui/projects/{slug}/delete")
    def ui_delete_project(request: Request, slug: str, csrf_token: str = Form("")):
        session, principal = _require_login(request, need=SCOPE_REVIEW)
        _check_csrf(session, csrf_token)
        if auth is None:
            return RedirectResponse("/ui/projects", status_code=303)
        # 기본 프로젝트는 삭제 금지(모든 사용자 공개·문서 폴백 대상).
        if slug == service.config.default_project:
            return _render(request, "project_detail.html", session, principal,
                           _project_detail_ctx(slug, error="기본 프로젝트는 삭제할 수 없습니다."),
                           status_code=400)
        auth.delete_project(slug, actor_id=principal.user_id)
        return RedirectResponse("/ui/projects", status_code=303)

    return app


def main() -> None:  # pragma: no cover - 구동 진입점
    import uvicorn

    from ..auth.service import build_auth_service

    config = Config.load_default()
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", str(config.repo_root))
    service = KnowledgeService(root, config)
    auth = build_auth_service(config, root) if config.auth.enabled else None
    host = os.environ.get("HOST", "127.0.0.1")  # 배포(컨테이너)는 0.0.0.0
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_web_app(service, auth), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
