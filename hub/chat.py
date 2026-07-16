"""멀티턴 AI 생성 오케스트레이터 — 펑션콜/도구 호출 (구현스펙-멀티턴생성-펑션콜.md).

사용자가 AI 와 대화하며 문서를 다듬고, "적용" 시 변경 제안(changeset)을 승인 큐에 제출한다.
읽기/컨텍스트 도구는 대화 중 실시간 실행되고, 변경(create/edit/deprecate)은 **staged** 로만 캡처된다.
실제 쓰기는 절대 여기서 하지 않는다 — 사람이 apply → submit_change(승인 큐) → 관리자 승인 → save.

세션은 **프로세스 인메모리**(휘발성 워크플로우 상태 — 지식 store/delta 와 무관). MVP 단일 프로세스 전제.
service 를 import 하지 않는다(순환 방지) — 오케스트레이터는 주입된 service 인스턴스로 위임한다.
"""

from __future__ import annotations

import json
import uuid

import yaml

from .store.normalize import normalize

# system 프롬프트 — generate_draft 규칙(§2-5 필수 섹션·대안/폐기·근거·환각 금지) + 도구 사용 지침.
SYSTEM = (
    "너는 팀의 설계 결정을 기록하는 문서 작성자다. 사용자와 대화하며 문서를 다듬는다. "
    "필요하면 먼저 도구로 기존 지식을 조사한다: search_knowledge 로 관련 문서를 찾고, "
    "get_document·get_history·get_related 로 맥락과 과거 결정을 확인하고, get_template 로 필수 구조를 확보한다. "
    "초안을 만들면 propose 전에 lint_check 로 검증해 필수 섹션(특히 ADR 의 대안·폐기 선택지)이 채워졌는지 확인한다. "
    "이 프로젝트의 핵심은 '왜'다 — 대안과 트레이드오프를 반드시 명시하고, 소스에 없는 사실을 지어내지 않는다. "
    "사용자가 적용/추가/수정/삭제를 원하면 그때만 propose_create/propose_edit/propose_deprecate 를 호출해 변경을 제안한다. "
    "제안은 사람이 확정(적용)해야 반영되며, 관리자 승인을 거친다. "
    "markdown 은 유효한 frontmatter + 본문만 담고 코드펜스로 감싸지 않는다."
)

# ── 도구 스키마 (OpenAI function-calling 포맷) ────────────────────────────
READ_TOOLS = [
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "유사 RAG 검색(FTS5). 관련 문서를 앵커 출처와 함께 찾는다.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "k": {"type": "integer", "default": 10}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_document",
        "description": "문서 원문 + frontmatter + 앵커 목록. 없으면 null.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                       "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "get_history",
        "description": "문서의 변경 이력(delta/근거/actor) 타임라인. 수정 시 과거 결정 근거화.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                       "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "get_related",
        "description": "계보 — supersedes 체인 + related. 중복·상충 결정 확인.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                       "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "list_documents",
        "description": "문서 목록 — type/status 필터.",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string"}, "status": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "get_template",
        "description": "타입별 스캐폴드 템플릿(adr/design-intent). 필수 섹션 구조 확보.",
        "parameters": {"type": "object", "properties": {"target_type": {"type": "string"}},
                       "required": ["target_type"]}}},
    {"type": "function", "function": {
        "name": "lint_check",
        "description": "초안 markdown 을 lint 게이트에 미리 통과시켜 {ok, reasons} 반환. 제안 전 자가 교정.",
        "parameters": {"type": "object", "properties": {"markdown": {"type": "string"}},
                       "required": ["markdown"]}}},
]

PROPOSE_TOOLS = [
    {"type": "function", "function": {
        "name": "propose_create",
        "description": "새 문서 초안을 변경 제안에 스테이징(아직 저장 안 함).",
        "parameters": {"type": "object", "properties": {
            "target_type": {"type": "string"}, "markdown": {"type": "string"}},
            "required": ["target_type", "markdown"]}}},
    {"type": "function", "function": {
        "name": "propose_edit",
        "description": "기존 문서 수정본을 변경 제안에 스테이징(아직 저장 안 함).",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"}, "markdown": {"type": "string"}},
            "required": ["doc_id", "markdown"]}}},
    {"type": "function", "function": {
        "name": "propose_deprecate",
        "description": "대상 문서를 폐기(status=deprecated)로 스테이징. 삭제는 하드 삭제가 아닌 폐기다.",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"}, "reason": {"type": "string"},
            "superseded_by": {"type": "string"}},
            "required": ["doc_id", "reason"]}}},
]

TOOLS = READ_TOOLS + PROPOSE_TOOLS
_READ_NAMES = {t["function"]["name"] for t in READ_TOOLS}
_PROPOSE_NAMES = {t["function"]["name"] for t in PROPOSE_TOOLS}


class ChatOrchestrator:
    """멀티턴 도구 루프 + 인메모리 세션. 주입된 service 로 읽기 도구를 위임한다."""

    def __init__(self, service, *, max_iters: int = 6):
        self.service = service
        self.max_iters = max_iters
        self.sessions: dict[str, dict] = {}

    # ── 세션 ──────────────────────────────────────────────────────────
    def new_session(
        self, *, actor: str = "anonymous", target_type: str = "adr",
        project: str | None = None, principal=None,
    ) -> str:
        sid = uuid.uuid4().hex
        self.sessions[sid] = {
            "messages": [{"role": "system", "content": SYSTEM}],
            "staged": [],
            "actor": actor,
            "target_type": target_type,
            "project": project,  # 이 세션이 작업 중인 프로젝트(읽기 도구·제안 스코프)
            "principal": principal,  # 읽기/쓰기 ACL 적용용(프로젝트 권한)
            # 생성 사용자에 고정(immutable) — 이후 턴/적용에서 호출자가 다르면 거부(세션 탈취 방지).
            "owner_user_id": getattr(principal, "user_id", None),
        }
        return sid

    def get(self, session_id: str) -> dict | None:
        return self.sessions.get(session_id)

    @staticmethod
    def _assert_owner(sess: dict, principal) -> None:
        """세션 소유자(생성 사용자)만 접근 허용. 다른 사용자가 기존 session_id 를 넘겨 세션·staged
        변경을 탈취하는 것을 막는다. 소유자 없는(로컬/무인증) 세션이나 principal 미지정은 통과."""
        owner = sess.get("owner_user_id")
        caller = getattr(principal, "user_id", None)
        if owner is not None and caller is not None and caller != owner:
            raise PermissionError("세션 소유자만 이 대화에 접근할 수 있습니다.")

    # ── 대화 턴 ────────────────────────────────────────────────────────
    def _prepare(self, session_id, user_message, actor, target_type, project, principal=None):
        if session_id is None or session_id not in self.sessions:
            session_id = self.new_session(
                actor=actor, target_type=target_type, project=project, principal=principal
            )
        sess = self.sessions[session_id]
        self._assert_owner(sess, principal)  # 세션 탈취 방지 — 소유자 불일치면 거부
        if principal is not None:
            sess["principal"] = principal  # 멤버십 변경 반영(동일 소유자일 때만 도달)
        sess["messages"].append({"role": "user", "content": user_message})
        return session_id, sess

    def _tool_phase(self, sess: dict, client):
        """펑션콜(도구 해결) 단계 — stream=False 로 반복하는 **제너레이터**.

        도구 호출마다 {"type":"tool",...} 이벤트를 yield 하고, 도구가 소진되면(=모델이 답할 준비)
        마지막 non-stream content(폴백)를 `return` 한다(StopIteration.value). 이때 assistant 메시지는
        append 하지 않아 최종 답변을 스트리밍으로 재생성할 수 있게 둔다. 반복 상한 도달 시 None 반환."""
        for _ in range(self.max_iters):
            out = client.chat(sess["messages"], tools=TOOLS)
            calls = out.get("tool_calls") or []
            if not calls:
                return out.get("content") or ""
            sess["messages"].append(_assistant_wire(out))
            for tc in calls:
                yield {"type": "tool", "name": tc["name"],
                       "args": tc.get("arguments") or {}}
                content = self._exec_tool(sess, tc["name"], tc.get("arguments") or {})
                sess["messages"].append({
                    "role": "tool", "tool_call_id": tc["id"], "content": content,
                })
        return None

    def turn(
        self, session_id: str | None, user_message: str,
        *, actor: str = "anonymous", target_type: str = "adr",
        project: str | None = None, llm=None, principal=None,
    ) -> dict:
        """비스트리밍 1턴 → {session_id, reply, staged}. (테스트·no-JS 폴백 경로.)"""
        client = llm if llm is not None else self.service._llm_client()
        session_id, sess = self._prepare(
            session_id, user_message, actor, target_type, project, principal
        )
        fallback = _drain(self._tool_phase(sess, client))
        reply = fallback if fallback else "(대화를 정리해 주세요.)"
        sess["messages"].append({"role": "assistant", "content": reply})
        return {"session_id": session_id, "reply": reply, "staged": sess["staged"]}

    def turn_stream(
        self, session_id: str | None, user_message: str,
        *, actor: str = "anonymous", target_type: str = "adr",
        project: str | None = None, llm=None, principal=None,
    ):
        """스트리밍 1턴. 도구 해결(non-stream) 후 최종 답변을 stream=True 로 토큰 yield.

        이벤트 dict 를 순차 yield: {"type":"session"} → {"type":"tool",...}* →
        {"type":"token","text":...}* → {"type":"done","reply","staged","session_id"}.
        """
        client = llm if llm is not None else self.service._llm_client()
        session_id, sess = self._prepare(
            session_id, user_message, actor, target_type, project, principal
        )
        yield {"type": "session", "session_id": session_id}
        # 도구 해결 단계(stream=False) — 도구 진행 이벤트를 그대로 전달, 폴백 content 를 회수.
        fallback = yield from self._tool_phase(sess, client)
        # 최종 답변 스트리밍(tools=None → 반드시 텍스트로 답변).
        acc: list[str] = []
        for delta in client.chat_stream(sess["messages"], tools=None):
            acc.append(delta)
            yield {"type": "token", "text": delta}
        reply = "".join(acc) or (fallback or "(대화를 정리해 주세요.)")
        sess["messages"].append({"role": "assistant", "content": reply})
        yield {"type": "done", "reply": reply, "staged": sess["staged"],
               "session_id": session_id}

    # ── 적용 → 승인 큐 제출 ────────────────────────────────────────────
    def apply(self, session_id: str, *, actor: str, principal=None) -> list[dict]:
        """staged 변경을 각각 승인 큐에 제출(submit_change). 제출 후 staged 를 비운다."""
        sess = self.sessions.get(session_id)
        if not sess:
            raise KeyError(f"세션 없음: {session_id}")
        self._assert_owner(sess, principal)  # 세션 탈취 방지 — 소유자만 적용(제출) 가능
        principal = principal if principal is not None else sess.get("principal")
        # 성공한 항목만 staged 에서 제거한다. 중간 실패 시 이미 제출된 항목이 남아 있으면
        # 재시도(/chat/apply)가 그것들을 중복 제출하므로, 각 성공 직후 pop 한다(재시도 안전).
        submissions = []
        staged = sess["staged"]
        while staged:
            item = staged[0]
            sub = self.service.submit_change(
                item["markdown"], actor=actor, op=item["op"],
                doc_id=item.get("doc_id"), project=item.get("project"),
                intended_diff=item.get("intended_diff"), principal=principal,
            )
            submissions.append(sub)
            staged.pop(0)  # 성공 후에만 제거 → 실패 항목은 맨 앞에 남아 재시도가 그것부터 재개
        return submissions

    # ── 도구 실행 ──────────────────────────────────────────────────────
    def _exec_tool(self, sess: dict, name: str, args: dict) -> str:
        svc = self.service
        project = sess.get("project")  # 현재 프로젝트로 읽기 도구 스코프
        principal = sess.get("principal")  # 프로젝트 ACL 적용
        try:
            if name == "search_knowledge":
                res = svc.search_knowledge(
                    args.get("query", ""), None, args.get("k", 10),
                    project=project, principal=principal,
                )
            elif name == "get_document":
                res = svc.get_document(args.get("id", ""), principal=principal)
            elif name == "get_history":
                res = svc.get_history(args.get("id", ""), principal=principal)
            elif name == "get_related":
                res = svc.get_related(args.get("id", ""), principal=principal)
            elif name == "list_documents":
                filters = {k: v for k, v in (("type", args.get("type")),
                           ("status", args.get("status"))) if v}
                res = svc.list_documents(filters or None, project=project, principal=principal)
            elif name == "get_template":
                res = {"template": svc.read_template(args.get("target_type", ""))}
            elif name == "lint_check":
                res = svc.lint_markdown(args.get("markdown", ""))
            elif name == "propose_create":
                res = self._stage(sess, "create", target_type=args.get("target_type"),
                                  markdown=args.get("markdown", ""))
            elif name == "propose_edit":
                res = self._stage(sess, "edit", doc_id=args.get("doc_id"),
                                  markdown=args.get("markdown", ""))
            elif name == "propose_deprecate":
                res = self._stage_deprecate(
                    sess, args.get("doc_id"), args.get("reason", ""),
                    args.get("superseded_by"))
            else:
                res = {"error": f"알 수 없는 도구: {name}"}
        except Exception as e:  # 도구 실패는 대화로 회신(전체 턴을 죽이지 않음)
            res = {"error": str(e)}
        return json.dumps(res, ensure_ascii=False, default=str)

    def _stage(
        self, sess: dict, op: str, *, markdown: str, doc_id=None, target_type=None,
        intended_diff: str | None = None,
    ) -> dict:
        prelint = self.service.lint_markdown(markdown)
        item = {"op": op, "markdown": markdown, "doc_id": doc_id,
                "target_type": target_type, "project": sess.get("project"),
                "intended_diff": intended_diff, "prelint": prelint}
        sess["staged"].append(item)
        return {"staged": True, "op": op, "doc_id": doc_id,
                "intended_diff": intended_diff, "prelint": prelint}

    def _stage_deprecate(self, sess: dict, doc_id: str, reason: str, superseded_by) -> dict:
        # 프로젝트 ACL 적용 — 접근 불가 문서는 미존재와 동일(원문이 staged 로 유출되는 것 방지).
        raw = self.service.get_raw(doc_id, principal=sess.get("principal"))
        if raw is None:
            return {"error": f"문서 없음: {doc_id}"}
        markdown = _set_deprecated(raw, superseded_by)
        # reason 은 staged item 에 intended_diff(폐기 근거)로 실어 apply→submit_change→이력까지
        # 전달한다(승인 워크플로우·history 컨텍스트 보존). 반환 dict 에만 남겨두면 유실된다.
        return self._stage(
            sess, "deprecate", markdown=markdown, doc_id=doc_id,
            intended_diff=f"폐기 사유: {reason}" if reason else None,
        )


def _drain(gen):
    """제너레이터를 끝까지 소비하고 `return` 값(StopIteration.value)을 돌려준다.

    스트리밍 이벤트를 무시하는 비스트리밍 경로(turn)가 _tool_phase 의 폴백 content 만 취할 때 사용."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _assistant_wire(out: dict) -> dict:
    """chat() 반환을 OpenAI assistant 와이어 메시지로 — tool_calls arguments 를 재직렬화."""
    msg: dict = {"role": "assistant", "content": out.get("content")}
    calls = out.get("tool_calls") or []
    if calls:
        msg["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"],
                          "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)}}
            for c in calls
        ]
    return msg


def _set_deprecated(raw_markdown: str, superseded_by) -> str:
    """문서 원문의 frontmatter status 를 폐기 상태로 바꾼 markdown 재구성(폐기 = 삭제 아님).

    superseded_by 가 있으면 status='superseded', 없으면 'deprecated'. **supersedes 는 건드리지 않는다**
    — supersedes 는 "이 문서가 X 를 대체한다"는 정방향 필드이므로, 대체 문서(교체본)쪽에서 별도로
    supersedes: <이 문서> 를 다는 것이 옳다(계보는 get_related 가 역방향으로 계산). 본문은 유지."""
    nd = normalize(raw_markdown)
    fm = dict(nd.frontmatter)
    fm["status"] = "superseded" if superseded_by else "deprecated"
    front = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n\n{nd.body.strip()}\n"
