"""서비스 레이어 — MCP·HTTP 가 공통으로 부르는 단일 코어 (CLAUDE.md §5).

인터페이스(MCP 도구 / HTTP 엔드포인트)는 **이 서비스만 호출**하며 로직을 중복하지 않는다.
`actor` 는 **인자로** 받는다 — 인증은 인터페이스가 채우고, 서비스는 인증을 모른다.

기획안1 §11 대응표:
  search_knowledge / get_document / list_documents / get_history / get_docs_diff / save_document.
  get_related·ingest_source·curate 는 P11/P12 에서 구현(여기선 시그니처만 예약).

구현 Phase: P06.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import deque
from pathlib import Path

import yaml

from .auth.principal import SCOPE_REVIEW, Principal, require_scope, require_write
from .config import Config
from .llm import LLMClient, LLMUnavailable
from .models import CHANGE_TYPES, DOC_TYPES, SaveResult
from .store import anchors as anchors_mod
from .store.base import Store
from .store.lint import LintError, lint
from .store.normalize import normalize

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def build_store(root, config: Config) -> Store:
    """config 로 저장 백엔드 선택 (구현스펙-postgres-배포.md). 기본 file, 배포 postgres."""
    if config.storage == "postgres":
        from .store.pg_store import PostgresStore  # 지연 import (psycopg 선택 의존)

        return PostgresStore(config)
    from .store.file_store import FileStore

    return FileStore(root, config)


# 인제스천 신규 문서의 id 접두어 (id 정규식 `^[a-z]+-[0-9]{4}$` 와 정합).
_TYPE_PREFIX = {
    "reference": "ref", "note": "note", "guide": "guide",
    "spec": "spec", "adr": "adr", "design-intent": "di",
}


def _prefix_for(doc_type: str) -> str:
    return _TYPE_PREFIX.get(doc_type, "ref")


def _validate_change_type(change_type: str | None) -> None:
    """명시 change_type 은 허용 enum 만. 클라이언트가 임의 문자열/위조 타입을 이력에 심는 것을 차단.

    None(미지정)은 통과 — 저장 엔진이 스냅샷·상태 전이로 자동 판정한다(외부 인터페이스 기본 경로)."""
    if change_type and change_type not in CHANGE_TYPES:
        raise LintError([f"허용되지 않은 change_type: {change_type!r} (허용: {list(CHANGE_TYPES)})"])


def _version_hash(raw_markdown: str | None) -> str | None:
    """정규화된 문서 내용(frontmatter + 본문)의 sha256 — 단 `updated` 는 제외.

    승인 워크플로우의 낙관적 충돌·멱등 복구 판별용 '버전 토큰'. reflect 가 매번 갱신하는
    `updated` 만 빼면 결정론적이라, 반영 시점과 무관하게 두 문서의 '의미 있는 내용'이 같은지
    비교할 수 있다. **본문만 보는 body_hash 로는 부족** — 폐기(status 만 바뀌는 frontmatter-only
    변경)를 감지 못하기 때문이다. 문서 없음/깨진 YAML 이면 None."""
    if raw_markdown is None:
        return None
    try:
        nd = normalize(raw_markdown)
    except yaml.YAMLError:
        return None
    fm = {k: v for k, v in nd.frontmatter.items() if k != "updated"}
    canonical = json.dumps(fm, sort_keys=True, ensure_ascii=False, default=str) + "\n" + nd.body
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _today() -> str:
    return datetime.date.today().isoformat()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _build_ingest_markdown(doc_id, doc_type, title, created, source_ref, content) -> str:
    # frontmatter 는 반드시 YAML 로 직렬화 — title/source 에 콜론 등 YAML 특수문자가 있어도
    # 스칼라로 안전하게 이스케이프된다(문자열 보간 시 깨진 YAML 로 저장 실패, C7).
    fm = {
        "id": doc_id, "type": doc_type, "title": title,
        "status": "accepted", "created": created, "source": source_ref,
    }
    front = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n\n{content.strip()}\n"


# ── AI 생성 (M8) 프롬프트 조립 (구현스펙-generate-M8.md §3, v0) ────────
def _read_template(target_type: str) -> str:
    # target_type 은 템플릿 경로에 결합되므로 허용 doc 타입만 통과(traversal 차단 — '../README' 등이
    # 리포의 임의 *.md 를 LLM 프롬프트로 끌어오는 것을 막는다). 그 외는 기존 "없음" 관용대로 빈 문자열.
    if target_type not in DOC_TYPES:
        return ""
    tp = _TEMPLATES_DIR / f"{target_type}.md"
    return tp.read_text(encoding="utf-8") if tp.exists() else ""


def _query_tokens(query: str) -> list[str]:
    """자유 입력 → dialect-중립 토큰(\\w+). 백엔드가 AND/OR 로 결합·이스케이프.

    'foo-bar OR "x"' → ['foo','bar','OR','x'] (연산자·특수문자는 리터럴 토큰으로 무력화)."""
    return re.findall(r"\w+", query or "")


def _related_tokens(hint, sources) -> list[str]:
    """힌트+소스 텍스트에서 키워드(\\w+, len≥2, 순서보존 dedup, 상한 12) — OR 검색용."""
    text = " ".join([hint or ""] + [(s.get("text") or "") for s in sources])
    toks = [t for t in re.findall(r"\w+", text) if len(t) >= 2]
    return list(dict.fromkeys(toks))[:12]


def _build_generate_prompt(target_type, template_text, related_ctx, source_texts, hint):
    system = (
        "너는 팀의 설계 결정을 기록하는 문서 작성자다. 목표 타입의 템플릿 구조를 정확히 따른다. "
        "필수 섹션을 모두 채운다. 대안과 폐기된 선택지, 트레이드오프를 반드시 명시한다 — "
        "이 프로젝트의 핵심은 '왜'다. 소스에 없는 사실을 지어내지 않는다. 불확실하면 '> [확인 필요]'로 "
        "표시한다. 출력은 유효한 frontmatter + 마크다운 본문만. 설명·인사말·코드펜스 금지."
    )
    parts = [f"[목표 타입] {target_type}", "[템플릿]", template_text]
    if related_ctx:
        parts.append("[관련 기존 ADR — 일관성 참고]")
        parts.extend(related_ctx)
    parts.append("[소스 자료]")
    parts.extend(source_texts or ["(없음)"])
    parts.append(f"[지시] {hint or '위 소스에 근거해 초안을 작성한다.'}")
    return system, "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    """LLM 이 ```로 감싼 경우 코드펜스를 제거한다(출력 계약 §4)."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t + "\n"


def _chain(start: str, adj: dict) -> list[str]:
    """start 에서 인접맵을 따라간 전이 체인(BFS, 순환 방지, 결정론적 순서)."""
    order: list[str] = []
    seen = {start}
    queue = deque(sorted(adj.get(start, set())))
    while queue:
        n = queue.popleft()
        if n in seen:
            continue
        seen.add(n)
        order.append(n)
        for m in sorted(adj.get(n, set())):
            if m not in seen:
                queue.append(m)
    return order


class KnowledgeService:
    """저장소 코어를 감싸는 얇은 파사드. 인터페이스 독립."""

    def __init__(self, root, config: Config | None = None, *, llm=None, store: Store | None = None):
        self.root = Path(root)
        self.config = config or Config()
        # 영속은 pluggable 스토어에 위임(FileStore 기본 · PostgresStore 배포). 주입 가능(테스트).
        self.store = store if store is not None else build_store(self.root, self.config)
        self._llm = llm  # 주입된 LLM 클라이언트(테스트/커스텀). 없으면 config 로 생성.
        self._chat = None  # 멀티턴 오케스트레이터(지연 생성 — 인메모리 세션 보관)

    def _llm_client(self):
        return self._llm if self._llm is not None else LLMClient(self.config.llm)

    def _orchestrator(self):
        if self._chat is None:
            from .chat import ChatOrchestrator  # 지연 import (순환 방지)

            self._chat = ChatOrchestrator(self)
        return self._chat

    def close(self) -> None:
        self.store.close()

    # ── 멀티프로젝트 헬퍼 (구현스펙-멀티프로젝트.md) ────────────────────
    def _resolve_project(self, raw_markdown: str, project: str | None) -> str:
        """유효 project 결정: 인자 우선 → frontmatter → 기본 프로젝트."""
        if project:
            return project
        try:
            fmp = normalize(raw_markdown).frontmatter.get("project")
        except Exception:
            fmp = None
        return fmp or self.config.default_project

    def _inject_project(self, raw_markdown: str, resolved: str) -> str:
        """비기본 project 를 frontmatter 에 기록(파일이 원천 → reconcile 안전).

        기본 프로젝트는 frontmatter 를 건드리지 않는다(인덱스 coercion 이 처리 — 기존 파일/테스트 무영향).
        """
        if resolved == self.config.default_project:
            return raw_markdown
        try:
            nd = normalize(raw_markdown)
        except yaml.YAMLError:
            return raw_markdown  # 파싱 실패는 이후 save/submit 의 lint 게이트가 처리
        if nd.frontmatter.get("project") == resolved:
            return raw_markdown
        fm = dict(nd.frontmatter)
        fm["project"] = resolved
        front = yaml.safe_dump(
            fm, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        return f"---\n{front}---\n\n{nd.body.strip()}\n"

    # ── 프로젝트 ACL 헬퍼 (필터-선행 §2-6) ─────────────────────────────
    def _scope_readable(self, filters: dict | None, principal: Principal | None) -> dict | None:
        """읽기 필터에 principal 의 접근 가능 프로젝트 집합을 주입(검색·목록 — store 호출 전)."""
        if principal is None:
            return filters
        readable = principal.readable_projects(self.config.default_project)
        if readable is None:  # admin/전권 → 필터 생략
            return filters
        return {**(filters or {}), "project__in": sorted(readable)}

    def _doc_readable(self, doc_id: str, principal: Principal | None) -> bool:
        """단건 문서의 project 를 principal 이 읽을 수 있는지(없는 문서는 통과 → 호출측 404)."""
        if principal is None:
            return True
        meta = self.store.get_meta(doc_id)
        if meta is None:
            return True
        proj = meta.get("project") or self.config.default_project
        return principal.can_read(proj, self.config.default_project)

    def _effective_project(self, raw_markdown: str, project: str | None) -> str:
        """실제로 저장될 프로젝트(= inject 후 frontmatter 기준). ACL 검사는 이 값으로 해야
        frontmatter 에 project 를 심어 스코프를 우회하는 것을 막는다.

        _inject_project 는 resolved!=default 일 때만 frontmatter 를 덮으므로, resolved==default 인
        경우 frontmatter 의 project 가 그대로 저장된다(둘의 괴리가 생기는 지점)."""
        resolved = self._resolve_project(raw_markdown, project)
        injected = self._inject_project(raw_markdown, resolved)
        try:
            fmp = normalize(injected).frontmatter.get("project")
        except Exception:
            fmp = None
        return fmp or self.config.default_project

    def _assert_can_write(self, resolved_project: str, principal: Principal | None) -> None:
        """쓰기 전 프로젝트 editor 권한 확인(principal 없으면 내부 호출 — 전권)."""
        if principal is None:
            return
        require_write(principal, resolved_project, self.config.default_project)

    def _current_project_of(self, doc_id: str | None) -> str | None:
        """기존 문서의 **현재** project(없으면 None). 전역 id 는 프로젝트 간 유일하지 않으므로
        같은 id 의 기존 문서가 다른 프로젝트에 있을 수 있다."""
        if not doc_id:
            return None
        meta = self.store.get_meta(doc_id)
        if meta is None:
            return None
        return meta.get("project") or self.config.default_project

    def _assert_can_move(
        self, doc_id: str | None, dest_project: str, principal: Principal | None
    ) -> None:
        """기존 문서를 다른 프로젝트로 옮기는 쓰기라면 **원본 프로젝트에도** editor 권한을 요구한다.

        전역 id 가 같은 타 프로젝트 문서를 목적지 project 만 검사해 덮어쓰며 이동시키는 우회를
        차단한다(크로스프로젝트 이동 = 원본·목적지 양쪽 쓰기권한 필요). principal 없으면 내부 전권."""
        if principal is None:
            return
        origin = self._current_project_of(doc_id)
        if origin is not None and origin != dest_project:
            require_write(principal, origin, self.config.default_project)

    # ── 쓰기 ──────────────────────────────────────────────────────────
    def save_document(
        self,
        raw_markdown: str,
        *,
        actor: str,
        change_type: str | None = None,
        intended_diff: str | None = None,
        project: str | None = None,
        now: str | None = None,
        principal: Principal | None = None,
    ) -> SaveResult | dict:
        """모든 쓰기의 진입점. 승인 게이트가 켜져 있으면 즉시 반영 대신 **승인 큐에 제출**하고
        제출 dict 를 반환한다(구현스펙-승인워크플로우.md). 꺼져 있으면 즉시 반영해 SaveResult 를 반환.

        실제 반영은 언제나 `_reflect`(store.save_document 단일 경로)만 수행한다(CLAUDE.md §2-1).
        project 는 leaf(submit_change/_reflect)에서 frontmatter 에 반영한다.
        """
        _validate_change_type(change_type)
        # 프로젝트 쓰기 권한 확인(editor). frontmatter 우회 방지 위해 '실제 저장될' 프로젝트로 검사.
        eff = self._effective_project(raw_markdown, project)
        self._assert_can_write(eff, principal)
        # 기존 문서를 다른 프로젝트로 옮기는 경우 원본 프로젝트 쓰기권한도 요구(우회 차단).
        try:
            moving_id = normalize(raw_markdown).id
        except Exception:
            moving_id = None
        self._assert_can_move(moving_id, eff, principal)
        if self.config.approval.enabled:
            # 기존 문서 여부로 op 결정 — 관리자가 승인함에서 변경 성격(생성/편집)을 정확히 보도록.
            op = "edit" if (moving_id and self.store.get_meta(moving_id) is not None) else "create"
            return self.submit_change(
                raw_markdown, actor=actor, op=op, change_type=change_type,
                intended_diff=intended_diff, project=project, now=now, principal=principal,
            )
        return self._reflect(
            raw_markdown,
            actor=actor,
            change_type=change_type,
            intended_diff=intended_diff,
            project=project,
            now=now,
        )

    def _reflect(
        self,
        raw_markdown: str,
        *,
        actor: str,
        change_type: str | None = None,
        intended_diff: str | None = None,
        project: str | None = None,
        now: str | None = None,
    ) -> SaveResult:
        """실제 반영 — store.reflect(신뢰 경로). 승인 시·게이트 off 에서만 호출된다."""
        resolved = self._effective_project(raw_markdown, project)
        raw_markdown = self._inject_project(raw_markdown, resolved)
        return self.store.reflect(
            raw_markdown,
            actor=actor,
            change_type=change_type,
            intended_diff=intended_diff,
            now=now,
        )

    # ── 승인 워크플로우 (구현스펙-승인워크플로우.md) ────────────────────
    def submit_change(
        self,
        raw_markdown: str,
        *,
        actor: str,
        op: str = "create",
        doc_id: str | None = None,
        change_type: str | None = None,
        intended_diff: str | None = None,
        project: str | None = None,
        now: str | None = None,
        principal: Principal | None = None,
    ) -> dict:
        """쓰기를 승인 대기 큐에 제출. 지식 store·인덱스는 건드리지 않는다.

        doc_id 는 markdown frontmatter 에서 도출(인자로 온 값 우선). id 없으면 LintError.
        제출 시점 참고용 prelint 를 함께 저장(권위 있는 lint 는 승인 시 실제 save 가 수행).
        """
        _validate_change_type(change_type)
        now_ts = now or _now()
        # '실제 저장될' 프로젝트로 통일(제출 메타·저장·ACL 검사 일치, frontmatter 우회 방지).
        resolved_project = self._effective_project(raw_markdown, project)
        self._assert_can_write(resolved_project, principal)
        raw_markdown = self._inject_project(raw_markdown, resolved_project)
        try:
            nd = normalize(raw_markdown, now=now_ts)
        except yaml.YAMLError as e:
            raise LintError([f"frontmatter YAML 파싱 실패: {e}"]) from e
        resolved_id = doc_id or nd.id
        if not resolved_id:
            raise LintError(["frontmatter 에 id 가 없습니다 — 제출 불가"])
        # target id(인자)와 문서 frontmatter id 가 어긋나면 거부 — 승인 시 실제 쓰기 대상은
        # frontmatter id 이므로, 큐에 표시된 doc_id 와 반영 결과가 달라지는 것을 막는다.
        if doc_id and nd.id and doc_id != nd.id:
            raise LintError([f"제출 target id 불일치: 지정 {doc_id} ≠ 문서 frontmatter {nd.id}"])
        # 기존 문서를 다른 프로젝트로 옮기는 제출이면 원본 프로젝트 쓰기권한도 요구(우회 차단).
        self._assert_can_move(resolved_id, resolved_project, principal)
        # 제출 시점 문서 버전(내용 해시)을 기준값으로 캡처(신규 문서는 None). 승인 시 현재 버전과
        # 대조해, 같은 기준에서 갈라진 다른 제출이 먼저 승인됐다면 이 제출을 충돌로 거부한다
        # (동일 문서 동시 편집이 서로를 덮어써 승인된 변경이 유실되는 것 방지).
        base_hash = _version_hash(self.store.get_raw(resolved_id))
        sub = self.store.create_submission(
            op=op,
            doc_id=resolved_id,
            raw_markdown=raw_markdown,
            intended_diff=intended_diff,
            change_type=change_type,
            project=resolved_project,
            actor=actor,
            prelint=self._prelint(raw_markdown),
            now=now_ts,
            base_hash=base_hash,
        )
        return {"submission_id": sub["id"], "status": sub["status"],
                "doc_id": sub["doc_id"], "op": sub["op"],
                "project": sub["project"], "prelint": sub["prelint"]}

    def list_submissions(
        self, status: str | None = None, *, project: str | None = None
    ) -> list[dict]:
        """승인 대기/처리된 제출 목록(최신 먼저). status·project 로 필터.

        project 미지정(None) 제출은 기본 프로젝트로 취급(문서 coercion 과 동일 — 레거시 제출 노출 보장).
        """
        subs = self.store.list_submissions(status)
        if project is None:
            return subs
        default = self.config.default_project
        return [s for s in subs if (s.get("project") or default) == project]

    def list_projects(self, *, principal: Principal | None = None) -> list[str]:
        """인덱스에 존재하는 project 목록(UI 셀렉터·API용). principal 이 접근 가능한 것만."""
        projs = self.store.list_projects()
        if principal is None:
            return projs
        readable = principal.readable_projects(self.config.default_project)
        if readable is None:
            return projs
        return [p for p in projs if p in readable]

    def get_submission(self, sub_id: str) -> dict | None:
        return self.store.read_submission(sub_id)

    def approve_submission(
        self, sub_id: str, *, principal: Principal, now: str | None = None
    ) -> SaveResult:
        """knowledge:review scope(admin)만 승인 가능. pending 제출을 실제 반영하고 approved 로 표기.

        인가는 인터페이스 독립 Principal + 공유 policy 로 강제(제거한 config.is_admin 대체).
        lint 실패 시 제출은 pending 유지, 지식 store 는 변경되지 않는다(LintError 전파).
        """
        require_scope(principal, SCOPE_REVIEW)
        approver = principal.username
        now_ts = now or _now()
        with self.store.submissions_lock():
            sub = self.store.read_submission(sub_id)
            if sub is None:
                raise KeyError(f"제출 없음: {sub_id}")
            if sub["status"] != "pending":
                raise ValueError(f"이미 처리된 제출: {sub_id} ({sub['status']})")
            # 리뷰어는 해당 제출의 프로젝트에 쓰기 권한이 있어야 한다(admin=전권).
            dest_project = sub.get("project") or self.config.default_project
            require_write(principal, dest_project, self.config.default_project)
            # 승인 시점에도 원본(현재 문서의) 프로젝트가 목적지와 다르면 그 프로젝트 쓰기권한을
            # 요구한다 — 크로스프로젝트 이동을 원본·목적지 양쪽 권한 없이 승인하지 못하게(우회 차단).
            self._assert_can_move(sub["doc_id"], dest_project, principal)
            current_raw = self.store.get_raw(sub["doc_id"])
            current_hash = _version_hash(current_raw)
            # 멱등 복구 — 반영(_reflect 커밋)은 됐는데 상태 갱신 전에 크래시하면 문서는 적용됐고
            # 제출만 pending 으로 남는다(P2-5). 현재 문서가 이미 이 제출 내용과 동일하면 다시
            # 반영하지 않고 상태 전이만 마무리한다. UI 에서 재승인하면 이 경로로 안전하게 수렴한다.
            if current_raw is not None and current_hash == _version_hash(sub["raw_markdown"]):
                res = SaveResult(
                    id=sub["doc_id"], change_type="noop", anchors_changed=[],
                    history_id=None,
                    warnings=["이미 반영된 내용 — 상태만 승인 처리(멱등 복구)"],
                )
                self.store.set_submission_status(
                    sub_id, status="approved", reviewer=approver, note=None, now=now_ts,
                )
                return res
            # 낙관적 충돌 검사 — 제출 기준 버전과 현재 문서 버전이 다르면(그 사이 다른 제출이
            # 승인돼 문서가 바뀌었으면) 거부한다. 이 제출을 그대로 반영하면 먼저 승인된 변경이
            # 조용히 사라진다(lost update). 기준값이 없는 레거시 제출('base_hash' 키 부재)은 스킵.
            if "base_hash" in sub and current_hash != sub["base_hash"]:
                raise ValueError(
                    f"제출 기준 버전 불일치(충돌): {sub_id} — 문서가 제출 이후 변경됨. "
                    "최신 문서로 다시 작성해 제출하세요."
                )
            # 실제 반영 — actor 는 원제출자(프로버넌스 보존). lint 실패 시 예외 전파(pending 유지).
            res = self._reflect(
                sub["raw_markdown"],
                actor=sub["actor"],
                change_type=sub.get("change_type"),
                intended_diff=sub.get("intended_diff"),
                project=sub.get("project"),
                now=now_ts,
            )
            self.store.set_submission_status(
                sub_id, status="approved", reviewer=approver, note=None, now=now_ts,
            )
            return res

    def reject_submission(
        self, sub_id: str, *, principal: Principal, note: str = "", now: str | None = None
    ) -> dict:
        """knowledge:review scope(admin)만 반려 가능. pending 제출을 rejected 로 표기(반영 없음)."""
        require_scope(principal, SCOPE_REVIEW)
        approver = principal.username
        now_ts = now or _now()
        with self.store.submissions_lock():
            sub = self.store.read_submission(sub_id)
            if sub is None:
                raise KeyError(f"제출 없음: {sub_id}")
            if sub["status"] != "pending":
                raise ValueError(f"이미 처리된 제출: {sub_id} ({sub['status']})")
            require_write(principal, sub.get("project") or self.config.default_project,
                          self.config.default_project)
            return self.store.set_submission_status(
                sub_id, status="rejected", reviewer=approver, note=note, now=now_ts,
            )

    # ── 읽기 ──────────────────────────────────────────────────────────
    def search_knowledge(
        self, query: str, filters: dict | None = None, k: int = 10,
        *, project: str | None = None, principal: Principal | None = None,
    ) -> list[dict]:
        """유사 RAG 검색(필터→FTS→bm25). 결과에 출처(id+anchor)+frontmatter 요약.

        project 지정 시 그 프로젝트로 스코프. principal 접근 가능 프로젝트로 ACL 필터(둘 다 필터-선행 §2-6).
        None 이면 전체(접근 가능 범위)."""
        # 자유 입력을 dialect-중립 토큰으로(구문 특수문자로 500 나는 것 방지, C6). 결합은 스토어가.
        tokens = _query_tokens(query)
        if not tokens:
            return []
        if project is not None:
            filters = {**(filters or {}), "project": project}
        filters = self._scope_readable(filters, principal)  # ACL: 검색 전 프로젝트 집합 제한
        hits = self.store.search(tokens, filters, k, mode="and")
        out: list[dict] = []
        for h in hits:
            meta = self.store.get_meta(h.doc_id) or {}
            out.append(
                {
                    "doc_id": h.doc_id,
                    "anchor": h.anchor,
                    "text": h.text,
                    "score": h.score,
                    "title": meta.get("title"),
                    "type": meta.get("type"),
                    "status": meta.get("status"),
                }
            )
        return out

    def get_document(self, doc_id: str, *, principal: Principal | None = None) -> dict | None:
        """문서 원문 + frontmatter + 앵커 목록. 없거나 접근 불가면 None. (백엔드 중립 조립.)"""
        raw = self.store.get_raw(doc_id)
        if raw is None:
            return None
        nd = normalize(raw)
        if principal is not None:
            proj = nd.frontmatter.get("project") or self.config.default_project
            if not principal.can_read(proj, self.config.default_project):
                return None  # 접근 불가 → 존재 노출 없이 404
        anchs = [
            {"slug": a.slug, "text": a.text, "level": a.level, "path": a.path}
            for a in anchors_mod.parse_anchors(nd.body)
        ]
        return {
            "id": nd.id,
            "type": nd.frontmatter.get("type"),
            "title": nd.frontmatter.get("title"),
            "status": nd.frontmatter.get("status"),
            "project": nd.frontmatter.get("project") or self.config.default_project,
            "tags": nd.frontmatter.get("tags", []),
            "related": nd.frontmatter.get("related", []),
            "supersedes": nd.frontmatter.get("supersedes"),
            "created": nd.frontmatter.get("created"),
            "updated": nd.frontmatter.get("updated"),
            "path": (self.store.get_meta(doc_id) or {}).get("path"),
            "body": nd.body,
            "anchors": anchs,
        }

    def get_raw(self, doc_id: str, *, principal: Principal | None = None) -> str | None:
        """문서의 정규화된 원문(마크다운)을 그대로 반환 — 편집 UI 로 로드용. 없거나 접근 불가면 None."""
        if not self._doc_readable(doc_id, principal):
            return None
        return self.store.get_raw(doc_id)

    def list_documents(
        self, filters: dict | None = None, *, limit: int | None = None, offset: int = 0,
        project: str | None = None, principal: Principal | None = None,
    ) -> list[dict]:
        """문서 메타 목록. project 지정 시 그 프로젝트로 스코프. principal 접근 가능 범위로 ACL 필터."""
        if project is not None:
            filters = {**(filters or {}), "project": project}
        filters = self._scope_readable(filters, principal)  # ACL(필터-선행)
        return self.store.list_documents(filters, limit=limit, offset=offset)

    def get_history(
        self, doc_id: str, *, anchor: str | None = None, limit: int | None = None,
        principal: Principal | None = None,
    ) -> list[dict]:
        """delta/summary/actor 타임라인(시간순). anchor 필터·limit(최근 N) 지원. ACL: 접근 불가면 빈 목록."""
        # 존재하지 않는 id 는 ACL 을 통과하지만(=미존재→통과), read_history 가 id 를 파일 경로로
        # 그대로 쓰므로 traversal(예: '../history/adr-0001') 로 타 프로젝트 이력이 노출될 수 있다.
        # 메타 없는 id 는 즉시 빈 목록으로 종료(존재 노출·경로 주입 차단).
        if self.store.get_meta(doc_id) is None:
            return []
        if not self._doc_readable(doc_id, principal):
            return []
        entries = self.store.read_history(doc_id)
        if anchor is not None:
            entries = [e for e in entries if e["anchor"] == anchor]
        if limit is not None:
            entries = entries[-limit:]
        return entries

    def get_docs_diff(
        self, doc_id: str, *, date: str | None = None, principal: Principal | None = None
    ) -> list[dict]:
        """의도된 변경(docs-diff) 목록. date 지정 시 해당 날짜만. ACL: 접근 불가면 빈 목록."""
        # 미존재 id 는 ACL 통과 후 glob 패턴('<id>.*.md')으로 쓰이므로 '*' 같은 입력이 전 프로젝트
        # docs-diff 를 훑을 수 있다. 메타 없는 id 는 즉시 빈 목록으로 종료(경로/glob 주입 차단).
        if self.store.get_meta(doc_id) is None:
            return []
        if not self._doc_readable(doc_id, principal):
            return []
        return self.store.read_docs_diff(doc_id, date=date)

    # ── 계보 (P11) ────────────────────────────────────────────────────
    def get_related(self, doc_id: str, *, principal: Principal | None = None) -> dict | None:
        """결정의 계보. supersedes 는 체인(정방향/역방향), related 는 직접 양방향. 순환 방지.

        반환: {id, supersedes[], superseded_by[], related[]}. 문서 없거나 접근 불가면 None.
        계보에 포함된 타 문서 id 는 principal 이 읽을 수 있는 것만 노출(교차 프로젝트 누출 방지).
        """
        if self.store.get_meta(doc_id) is None:
            return None
        if not self._doc_readable(doc_id, principal):
            return None

        supersedes, related = self._relation_maps()
        superseded_by: dict[str, set] = {}
        for src, targets in supersedes.items():
            for t in targets:
                superseded_by.setdefault(t, set()).add(src)

        rel = set(related.get(doc_id, set()))
        for other, s in related.items():  # 들어오는 related (양방향)
            if doc_id in s:
                rel.add(other)
        rel.discard(doc_id)

        def _visible(ids: list[str]) -> list[str]:
            if principal is None:
                return ids
            return [i for i in ids if self._doc_readable(i, principal)]

        return {
            "id": doc_id,
            "supersedes": _visible(_chain(doc_id, supersedes)),
            "superseded_by": _visible(_chain(doc_id, superseded_by)),
            "related": _visible(sorted(rel)),
        }

    def _relation_maps(self) -> tuple[dict, dict]:
        """모든 문서의 frontmatter 를 읽어 supersedes/related 인접맵을 만든다."""
        supersedes: dict[str, set] = {}
        related: dict[str, set] = {}
        for doc_id, fm in self.store.all_frontmatter().items():
            sup = fm.get("supersedes")
            supersedes[doc_id] = (
                set(sup) if isinstance(sup, list) else ({sup} if sup else set())
            )
            related[doc_id] = set(fm.get("related") or [])
        return supersedes, related

    # ── 인제스천 (P12) — save 를 호출하는 얇은 어댑터 ([Δ] §10) ────────
    def ingest_source(
        self,
        source_ref: str,
        *,
        content: str,
        actor: str = "ingest",
        doc_type: str = "reference",
        title: str | None = None,
        project: str | None = None,
        now: str | None = None,
        principal: Principal | None = None,
    ) -> SaveResult:
        """소스를 정규화해 save 경유 저장. source 키로 기존 문서를 찾아 **갱신(멱등)**, 없으면 신규.

        소스별 실파서(노션/시트)는 기획안2 — 여기선 content(마크다운 본문)를 받는 얇은 어댑터.
        멱등 조회는 project 로 스코프한다(같은 source_ref 라도 프로젝트가 다르면 별도 문서).
        """
        # source 조회 + 신규 id 채번 + save 를 전역 락으로 감싼다. 동시 신규 ingest 가
        # 같은 id 를 채번해 서로 덮어쓰는 것을 막는다(문서별 락은 동일 id 만 직렬화, C8).
        scoped = self._resolve_project("", project)  # 빈 md → 인자 or 기본 프로젝트
        self._assert_can_write(scoped, principal)  # 프로젝트 editor 권한(락 밖에서 조기 거부)
        with self.store.ingest_lock():
            # 승인 대기 제출도 source 조회·id 채번에 참여시킨다. 그렇지 않으면 승인 전 두 소스가
            # 같은 id 를 받아 나중 승인이 먼저 것을 덮어쓴다(멱등 붕괴, C8 확장).
            pending = self.store.list_submissions("pending")
            existing = self.store.list_documents(
                filters={"source": source_ref, "project": scoped}
            )
            pmatch = None if existing else self._pending_by_source(pending, source_ref, scoped)
            if existing:  # 멱등: 반영된 같은 source(동일 project) → 기존 문서 갱신
                meta = existing[0]
                doc_id = meta["id"]
                doc_type = meta["type"]
                created = normalize(self.store.get_raw(doc_id) or "").frontmatter.get("created", "")
            elif pmatch:  # 멱등: 대기 중 같은 source → 그 문서 id 로 갱신(중복 채번 방지)
                doc_id = pmatch["doc_id"]
                pfm = normalize(pmatch.get("raw_markdown") or "").frontmatter
                doc_type = pfm.get("type", doc_type)
                created = pfm.get("created", "") or (now or _today())[:10]
            else:  # 신규 — 반영 문서 + 대기 제출 id 를 모두 피해 채번
                doc_id = self._next_id(
                    _prefix_for(doc_type), extra_ids={s["doc_id"] for s in pending}
                )
                created = (now or _today())[:10]

            md = _build_ingest_markdown(
                doc_id, doc_type, title or source_ref, created, source_ref, content
            )
            return self.save_document(
                md, actor=actor, change_type="ingest", project=scoped, now=now,
                principal=principal,
            )

    def _next_id(self, prefix: str, *, extra_ids=None) -> str:
        """`prefix-NNNN` 형식의 다음 유일 id (기존 최대 번호 +1). ingest_lock 하에서 호출.

        extra_ids: 반영 전이라 인덱스에 없지만 이미 예약된 id(승인 대기 제출 등)도 회피 대상에 포함."""
        pat = re.compile(rf"^{re.escape(prefix)}-(\d{{4}})$")
        ids = list(self.store.all_doc_ids()) + list(extra_ids or [])
        nums = [int(m.group(1)) for i in ids if (m := pat.match(i))]
        return f"{prefix}-{(max(nums) + 1) if nums else 1:04d}"

    def _pending_by_source(self, pending: list[dict], source_ref: str, project: str) -> dict | None:
        """대기 중 제출에서 같은 source·project 를 찾아 반환(멱등 갱신 대상). 없으면 None."""
        for s in pending:
            if (s.get("project") or self.config.default_project) != project:
                continue
            try:
                fm = normalize(s.get("raw_markdown") or "").frontmatter
            except Exception:
                continue
            if fm.get("source") == source_ref:
                return s
        return None

    # ── curate (P12) — 옵션, LLM 미구성 시 graceful skip (기획안1 §8) ──
    def curate(self, query: str, candidate_ids: list[str], *, llm=None,
               principal: Principal | None = None) -> dict:
        """후보 섹션을 LLM 으로 압축. LLM 미구성 시 skip(요약 없이 후보 그대로). ACL: 접근 가능 문서만."""
        client = llm if llm is not None else self._llm_client()
        cands: list[dict] = []
        for cid in candidate_ids:
            doc = self.get_document(cid, principal=principal)
            if doc:
                cands.append({"id": cid, "title": doc["title"], "body": doc["body"]})

        if not client.available:
            return {"skipped": True, "summary": None, "candidate_ids": [c["id"] for c in cands]}

        blocks = "\n\n".join(f"## {c['id']} — {c['title']}\n{c['body']}" for c in cands)
        prompt = (
            f"질의: {query}\n\n아래 후보 문서들에서 질의에 답하는 핵심만 앵커 출처와 함께 "
            f"압축 요약하라. 사실만, 추측 금지.\n\n{blocks}"
        )
        summary = client.complete(prompt)
        return {"skipped": False, "summary": summary, "candidate_ids": [c["id"] for c in cands]}

    # ── AI 생성 (M8) — 초안 반환만, 저장은 사람 검토+lint+save ─────────
    def generate_draft(
        self,
        target_type: str,
        sources: list[dict] | None = None,
        hint: str | None = None,
        *,
        llm=None,
        related_k: int = 3,
        project: str | None = None,
        principal: Principal | None = None,
    ) -> dict:
        """소스 → LLM 초안(마크다운). **저장하지 않는다.** lint 를 미리 돌려 함께 반환.
        project 지정 시 관련 ADR 수집을 그 프로젝트로 스코프. principal 로 소스·관련문서 ACL 적용.

        반환: {draft_markdown, lint:{ok,reasons}, used_sources[], related_context[]}.
        LLM 미구성 시 LLMUnavailable (엔드포인트가 503 으로 매핑, 직접 작성은 항상 가능).
        """
        client = llm if llm is not None else self._llm_client()
        if not client.available:
            raise LLMUnavailable("LLM 미구성 — /generate 비활성")

        # 1. 소스 취합
        used: list[str] = []
        source_texts: list[str] = []
        for s in sources or []:
            if s.get("kind") == "doc":
                doc = self.get_document(s.get("id"), principal=principal)
                if doc:
                    source_texts.append(f"# {doc['title']}\n{doc['body']}")
                    used.append(doc["id"])
            else:  # upload | note
                text = (s.get("text") or "").strip()
                if text:
                    source_texts.append(text)
                    used.append(f"{s.get('kind', 'note')}:inline")

        # 2. 관련 기존 ADR 자동 수집 (유사 RAG, 벡터 없음).
        #    힌트/소스 키워드를 OR 로 묶어 넓게 후보를 잡는다(암묵 AND 로 0건 되는 것 방지).
        rel_tokens = _related_tokens(hint, sources or [])  # \w 토큰(len≥2, dedup, 상한)
        related_ids: list[str] = []
        if rel_tokens:
            # OR 모드로 넓게 후보 수집(암묵 AND 로 0건 되는 것 방지).
            rel_filters = {"type": "adr", "status": "accepted"}
            if project is not None:
                rel_filters["project"] = project
            rel_filters = self._scope_readable(rel_filters, principal)  # ACL(필터-선행)
            hits = self.store.search(rel_tokens, rel_filters, k=related_k * 3, mode="or")
            for h in hits:
                if h.doc_id not in used and h.doc_id not in related_ids:
                    related_ids.append(h.doc_id)
                if len(related_ids) >= related_k:
                    break
        related_ctx = []
        for rid in related_ids:
            d = self.get_document(rid, principal=principal)
            if d:
                related_ctx.append(f"## {d['id']} {d['title']}\n{d['body'][:400]}")

        # 3~4. 템플릿 + 프롬프트 → LLM
        system, user = _build_generate_prompt(
            target_type, _read_template(target_type), related_ctx, source_texts, hint
        )
        draft = _strip_fences(client.complete(user, system=system))

        # 5. 저장 전 lint 미리 실행(막지 않음 — 초안이므로 참고용)
        return {
            "draft_markdown": draft,
            "lint": self._prelint(draft),
            "used_sources": used,
            "related_context": related_ids,
        }

    # ── 멀티턴 AI 생성 (구현스펙-멀티턴생성-펑션콜.md) ─────────────────
    def chat_turn(
        self,
        session_id: str | None,
        user_message: str,
        *,
        actor: str = "anonymous",
        target_type: str = "adr",
        project: str | None = None,
        llm=None,
        principal: Principal | None = None,
    ) -> dict:
        """대화 1턴 → {session_id, reply, staged}. LLM 미구성 시 LLMUnavailable(503 매핑).
        project 는 세션에 저장돼 읽기 도구·제안이 그 프로젝트로 스코프된다. principal 로 ACL 적용."""
        client = llm if llm is not None else self._llm_client()
        if not client.available:
            raise LLMUnavailable("LLM 미구성 — 멀티턴 채팅 비활성")
        return self._orchestrator().turn(
            session_id, user_message, actor=actor, target_type=target_type,
            project=project, llm=client, principal=principal,
        )

    def chat_turn_stream(
        self,
        session_id: str | None,
        user_message: str,
        *,
        actor: str = "anonymous",
        target_type: str = "adr",
        project: str | None = None,
        llm=None,
        principal: Principal | None = None,
    ):
        """대화 1턴을 스트리밍(제너레이터). 도구 해결(stream=False) 후 최종 답변을 stream=True 로.

        이벤트 dict 를 yield: session/tool/token/done. LLM 미구성 시 LLMUnavailable(첫 소비 시 전파).
        """
        client = llm if llm is not None else self._llm_client()
        if not client.available:
            raise LLMUnavailable("LLM 미구성 — 멀티턴 채팅 비활성")
        return self._orchestrator().turn_stream(
            session_id, user_message, actor=actor, target_type=target_type,
            project=project, llm=client, principal=principal,
        )

    @property
    def llm_available(self) -> bool:
        """LLM 구성 여부(스트리밍 엔드포인트가 사전 503 가드에 사용)."""
        return self._llm_client().available

    def get_session(self, session_id: str) -> dict | None:
        """세션 상태(messages/staged 등). 없으면 None."""
        return self._orchestrator().get(session_id)

    def apply_session(
        self, session_id: str, *, actor: str, principal: Principal | None = None
    ) -> list[dict]:
        """세션의 staged 변경을 승인 큐에 제출(submit_change). 반환: 제출 목록. principal 로 쓰기 ACL."""
        return self._orchestrator().apply(session_id, actor=actor, principal=principal)

    def read_template(self, target_type: str) -> str:
        """타입별 스캐폴드 템플릿 원문(멀티턴 도구 get_template 위임). 없으면 빈 문자열."""
        return _read_template(target_type)

    def lint_markdown(self, markdown: str) -> dict:
        """초안을 lint 게이트에 미리 통과시켜 결과 반환(멀티턴 도구 lint_check 위임)."""
        return self._prelint(markdown)

    def _prelint(self, draft: str) -> dict:
        try:
            nd = normalize(draft)
        except yaml.YAMLError as e:
            return {"ok": False, "reasons": [f"frontmatter YAML 파싱 실패: {e}"]}
        try:
            lint(nd, self.config, exists_fn=self.store.exists)
            return {"ok": True, "reasons": []}
        except LintError as e:
            return {"ok": False, "reasons": e.reasons}
