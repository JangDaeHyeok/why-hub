"""서비스 레이어 — MCP·HTTP 가 공통으로 부르는 단일 코어 (CLAUDE.md §5).

인터페이스(MCP 도구 / HTTP 엔드포인트)는 **이 서비스만 호출**하며 로직을 중복하지 않는다.
`actor` 는 **인자로** 받는다 — 인증은 인터페이스가 채우고, 서비스는 인증을 모른다.

기획안1 §11 대응표:
  search_knowledge / get_document / list_documents / get_history / get_docs_diff / save_document.
  get_related·ingest_source·curate 는 P11/P12 에서 구현(여기선 시그니처만 예약).

구현 Phase: P06.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

import yaml

from .config import Config
from .llm import LLMClient, LLMUnavailable
from .models import SaveResult
from .store import anchors as anchors_mod
from .store import history as history_mod
from .store import paths
from .store import save as save_store
from .store.index_fts import open_index
from .store.lint import LintError, lint
from .store.locking import doc_lock
from .store.normalize import normalize

# ingest 신규 채번을 직렬화하는 전역 락 id (실제 문서 id 와 충돌하지 않는 sentinel).
_INGEST_LOCK_ID = "__ingest__"

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


# 인제스천 신규 문서의 id 접두어 (id 정규식 `^[a-z]+-[0-9]{4}$` 와 정합).
_TYPE_PREFIX = {
    "reference": "ref", "note": "note", "guide": "guide",
    "spec": "spec", "adr": "adr", "design-intent": "di",
}


def _prefix_for(doc_type: str) -> str:
    return _TYPE_PREFIX.get(doc_type, "ref")


def _today() -> str:
    import datetime

    return datetime.date.today().isoformat()


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
    tp = _TEMPLATES_DIR / f"{target_type}.md"
    return tp.read_text(encoding="utf-8") if tp.exists() else ""


def _fts_safe_query(query: str) -> str:
    """자유 입력 → 안전한 FTS5 질의. \\w 토큰만 추출해 각 토큰을 따옴표로 감싸 AND 결합.

    'foo-bar' → '"foo" "bar"', 'OR'/'"' 등 FTS 연산자·특수문자를 리터럴로 무력화한다.
    토큰이 없으면 빈 문자열(호출측이 빈 결과 처리).
    """
    toks = re.findall(r"\w+", query or "")
    return " ".join(f'"{t}"' for t in toks)


def _related_query(hint, sources) -> str:
    """힌트+소스 텍스트에서 키워드를 뽑아 OR 로 묶은 FTS 질의(\\w 토큰이라 안전)."""
    text = " ".join([hint or ""] + [(s.get("text") or "") for s in sources])
    toks = [t for t in re.findall(r"\w+", text) if len(t) >= 2]
    toks = list(dict.fromkeys(toks))[:12]  # 순서 보존 dedup, 상한
    return " OR ".join(toks)


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

    def __init__(self, root, config: Config | None = None, *, llm=None):
        self.root = Path(root)
        self.config = config or Config()
        self.index = open_index(self.root)  # 공유 인덱스 연결
        self._llm = llm  # 주입된 LLM 클라이언트(테스트/커스텀). 없으면 config 로 생성.

    def _llm_client(self):
        return self._llm if self._llm is not None else LLMClient(self.config.llm)

    def close(self) -> None:
        self.index.close()

    # ── 쓰기 ──────────────────────────────────────────────────────────
    def save_document(
        self,
        raw_markdown: str,
        *,
        actor: str,
        change_type: str | None = None,
        intended_diff: str | None = None,
        now: str | None = None,
    ) -> SaveResult:
        """모든 쓰기의 단일 진입점(store.save_document 경유). 공유 인덱스를 넘긴다."""
        return save_store.save_document(
            raw_markdown,
            root=self.root,
            actor=actor,
            config=self.config,
            change_type=change_type,
            intended_diff=intended_diff,
            index=self.index,
            now=now,
        )

    # ── 읽기 ──────────────────────────────────────────────────────────
    def search_knowledge(
        self, query: str, filters: dict | None = None, k: int = 10
    ) -> list[dict]:
        """유사 RAG 검색(필터→FTS→bm25). 결과에 출처(id+anchor)+frontmatter 요약."""
        # 사용자 자유 입력을 안전한 FTS 질의로 변환(구문 특수문자로 500 나는 것 방지, C6).
        safe = _fts_safe_query(query)
        if not safe:
            return []
        hits = self.index.search(safe, filters, k)
        out: list[dict] = []
        for h in hits:
            meta = self.index.get_meta(h.doc_id) or {}
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

    def get_document(self, doc_id: str) -> dict | None:
        """문서 원문 + frontmatter + 앵커 목록. 없으면 None."""
        meta = self.index.get_meta(doc_id)
        if not meta or not meta.get("path"):
            return None
        p = self.root / meta["path"]
        if not p.exists():
            return None
        nd = normalize(p.read_text(encoding="utf-8"))
        anchs = [
            {"slug": a.slug, "text": a.text, "level": a.level, "path": a.path}
            for a in anchors_mod.parse_anchors(nd.body)
        ]
        return {
            "id": nd.id,
            "type": nd.frontmatter.get("type"),
            "title": nd.frontmatter.get("title"),
            "status": nd.frontmatter.get("status"),
            "tags": nd.frontmatter.get("tags", []),
            "related": nd.frontmatter.get("related", []),
            "supersedes": nd.frontmatter.get("supersedes"),
            "created": nd.frontmatter.get("created"),
            "updated": nd.frontmatter.get("updated"),
            "path": meta["path"],
            "body": nd.body,
            "anchors": anchs,
        }

    def get_raw(self, doc_id: str) -> str | None:
        """문서의 정규화된 원문(마크다운)을 그대로 반환 — 편집 UI 로 로드용. 없으면 None."""
        meta = self.index.get_meta(doc_id)
        if not meta or not meta.get("path"):
            return None
        p = self.root / meta["path"]
        return p.read_text(encoding="utf-8") if p.exists() else None

    def list_documents(
        self, filters: dict | None = None, *, limit: int | None = None, offset: int = 0
    ) -> list[dict]:
        return self.index.list_documents(filters, limit=limit, offset=offset)

    def get_history(
        self, doc_id: str, *, anchor: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """delta/summary/actor 타임라인(시간순). anchor 필터·limit(최근 N) 지원."""
        entries = history_mod.read(doc_id, self.root)
        if anchor is not None:
            entries = [e for e in entries if e.anchor == anchor]
        if limit is not None:
            entries = entries[-limit:]
        return [
            {
                "ts": e.ts,
                "actor": e.actor,
                "type": e.type,
                "anchor": e.anchor,
                "summary": e.summary,
                "summary_source": e.summary_source,
                "delta": e.delta,
            }
            for e in entries
        ]

    def get_docs_diff(self, doc_id: str, *, date: str | None = None) -> list[dict]:
        """의도된 변경(docs-diff) 목록. date 지정 시 해당 날짜만."""
        d = paths.docs_diff_dir(self.root)
        if not d.exists():
            return []
        out: list[dict] = []
        prefix = f"{doc_id}."
        for f in sorted(d.glob(f"{doc_id}.*.md")):
            dt = f.name[len(prefix) : -3]  # '<id>.' 와 '.md' 제거
            if date is not None and dt != date:
                continue
            out.append({"date": dt, "content": f.read_text(encoding="utf-8")})
        return out

    # ── 계보 (P11) ────────────────────────────────────────────────────
    def get_related(self, doc_id: str) -> dict | None:
        """결정의 계보. supersedes 는 체인(정방향/역방향), related 는 직접 양방향. 순환 방지.

        반환: {id, supersedes[], superseded_by[], related[]}. 문서 없으면 None.
        """
        if self.index.get_meta(doc_id) is None:
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

        return {
            "id": doc_id,
            "supersedes": _chain(doc_id, supersedes),
            "superseded_by": _chain(doc_id, superseded_by),
            "related": sorted(rel),
        }

    def _relation_maps(self) -> tuple[dict, dict]:
        """모든 문서의 frontmatter 를 읽어 supersedes/related 인접맵을 만든다."""
        supersedes: dict[str, set] = {}
        related: dict[str, set] = {}
        for meta in self.index.list_documents():
            doc_id = meta["id"]
            p = self.root / meta["path"] if meta.get("path") else None
            fm = {}
            if p and p.exists():
                fm = normalize(p.read_text(encoding="utf-8")).frontmatter
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
        now: str | None = None,
    ) -> SaveResult:
        """소스를 정규화해 save 경유 저장. source 키로 기존 문서를 찾아 **갱신(멱등)**, 없으면 신규.

        소스별 실파서(노션/시트)는 기획안2 — 여기선 content(마크다운 본문)를 받는 얇은 어댑터.
        """
        # source 조회 + 신규 id 채번 + save 를 전역 락으로 감싼다. 동시 신규 ingest 가
        # 같은 id 를 채번해 서로 덮어쓰는 것을 막는다(문서별 락은 동일 id 만 직렬화, C8).
        with doc_lock(_INGEST_LOCK_ID, self.root, timeout=self.config.lock_timeout):
            existing = self.index.list_documents(filters={"source": source_ref})
            if existing:  # 멱등: 같은 source → 기존 문서 갱신
                meta = existing[0]
                doc_id = meta["id"]
                doc_type = meta["type"]
                prev = normalize((self.root / meta["path"]).read_text(encoding="utf-8"))
                created = prev.frontmatter.get("created", "")
            else:  # 신규
                doc_id = self._next_id(_prefix_for(doc_type))
                created = (now or _today())[:10]

            md = _build_ingest_markdown(
                doc_id, doc_type, title or source_ref, created, source_ref, content
            )
            return self.save_document(md, actor=actor, change_type="ingest", now=now)

    def _next_id(self, prefix: str) -> str:
        """`prefix-NNNN` 형식의 다음 유일 id (기존 최대 번호 +1). _INGEST_LOCK_ID 하에서 호출."""
        pat = re.compile(rf"^{re.escape(prefix)}-(\d{{4}})$")
        nums = [int(m.group(1)) for i in self.index.all_doc_ids() if (m := pat.match(i))]
        return f"{prefix}-{(max(nums) + 1) if nums else 1:04d}"

    # ── curate (P12) — 옵션, LLM 미구성 시 graceful skip (기획안1 §8) ──
    def curate(self, query: str, candidate_ids: list[str], *, llm=None) -> dict:
        """후보 섹션을 LLM 으로 압축. LLM 미구성 시 skip(요약 없이 후보 그대로)."""
        client = llm if llm is not None else self._llm_client()
        cands: list[dict] = []
        for cid in candidate_ids:
            doc = self.get_document(cid)
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
    ) -> dict:
        """소스 → LLM 초안(마크다운). **저장하지 않는다.** lint 를 미리 돌려 함께 반환.

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
                doc = self.get_document(s.get("id"))
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
        query = _related_query(hint, sources or [])  # 이미 안전한 \w 토큰의 OR 질의
        related_ids: list[str] = []
        if query:
            # 자체 OR 질의라 index.search 를 직접 호출(search_knowledge 의 토큰 재조합을 우회).
            hits = self.index.search(
                query, {"type": "adr", "status": "accepted"}, k=related_k * 3
            )
            for h in hits:
                if h.doc_id not in used and h.doc_id not in related_ids:
                    related_ids.append(h.doc_id)
                if len(related_ids) >= related_k:
                    break
        related_ctx = []
        for rid in related_ids:
            d = self.get_document(rid)
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

    def _prelint(self, draft: str) -> dict:
        try:
            nd = normalize(draft)
        except yaml.YAMLError as e:
            return {"ok": False, "reasons": [f"frontmatter YAML 파싱 실패: {e}"]}
        try:
            lint(nd, self.config, exists_fn=self.index.exists)
            return {"ok": True, "reasons": []}
        except LintError as e:
            return {"ok": False, "reasons": e.reasons}
