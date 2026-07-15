"""PostgresStore — 모든 지식 상태를 PostgreSQL 에 (배포 백엔드, 구현스펙-postgres-배포.md).

문서·스냅샷·이력·docs-diff·제출·FTS(tsvector)를 한 DB에. save 는 **단일 트랜잭션**으로 원자적이라
파일 백엔드의 저널/flock 이 필요 없다(문서 직렬화는 `pg_advisory_xact_lock`). 순수 도메인 모듈
(normalize/lint/anchors/diffing/history.build)은 파일 백엔드와 동일하게 재사용한다.

검색은 유사 RAG 유지 — `tsvector`/`ts_rank_cd`(임베딩 아님, CLAUDE.md §2-2). 필터-선행(§2-6).
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager

from ..config import Config
from ..models import Hit, SaveResult
from . import anchors as anchors_mod
from . import diffing
from . import history as history_mod
from . import submissions as _subs
from .base import Store
from .lint import LintError, lint
from .normalize import normalize

_META_COLS = ("id", "type", "status", "title", "source", "updated", "project", "tenant")

# 등가 필터 컬럼(파일 백엔드 _EQ_FILTERS 와 동일).
_EQ_FILTERS = ("type", "status", "project", "tenant", "source")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id text PRIMARY KEY, type text, status text, title text,
  project text, tenant text, source text, author text,
  created text, updated text,
  tags jsonb DEFAULT '[]'::jsonb, related jsonb DEFAULT '[]'::jsonb,
  supersedes text, body text, raw text, body_hash text,
  frontmatter jsonb DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS snapshots(
  doc_id text PRIMARY KEY, body text, body_hash text
);
CREATE TABLE IF NOT EXISTS history(
  seq bigserial PRIMARY KEY, doc_id text, ts text, actor text, type text,
  anchor text, summary text, summary_source text, delta text
);
CREATE INDEX IF NOT EXISTS history_doc_idx ON history(doc_id, seq);
CREATE TABLE IF NOT EXISTS docs_diff(
  doc_id text, date text, content text, PRIMARY KEY(doc_id, date)
);
CREATE TABLE IF NOT EXISTS chunks(
  doc_id text, anchor text, text text,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text,''))) STORED
);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin(tsv);
CREATE TABLE IF NOT EXISTS submissions(
  id text PRIMARY KEY, op text, doc_id text, raw_markdown text, intended_diff text,
  change_type text, project text, actor text, status text, prelint jsonb,
  created text, reviewer text, reviewed_at text, note text
);
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS base_hash text;
"""

# 락 종류별 advisory key (임의 상수).
_INGEST_KEY = 811001
_SUBMISSIONS_KEY = 811002


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class PostgresStore(Store):
    def __init__(self, config: Config):
        import psycopg  # 지연 import (선택 의존)

        self.config = config
        self.default_project = config.default_project
        self._lock = threading.RLock()  # 공유 커넥션 직렬화(프로세스 내) — 재진입 허용
        self.conn = psycopg.connect(config.postgres.resolve_dsn(), autocommit=True)
        with self._lock, self.conn.cursor() as cur:
            cur.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── 쓰기 (단일 트랜잭션 파이프라인) ───────────────────────────────
    def reflect(
        self, raw_markdown: str, *, actor: str, change_type: str | None = None,
        intended_diff: str | None = None, now: str | None = None,
    ) -> SaveResult:
        import datetime

        import yaml

        now_ts = now or datetime.datetime.now().isoformat(timespec="seconds")
        try:
            nd = normalize(raw_markdown, now=now_ts)
        except yaml.YAMLError as e:
            raise LintError([f"frontmatter YAML 파싱 실패: {e}"]) from e

        # lint 게이트 — 부작용 이전(실패 시 아무것도 안 씀). exists 는 DB 조회.
        lint(nd, self.config, exists_fn=self.exists)
        doc_id = nd.id
        doc_type = nd.frontmatter.get("type")
        warnings: list[str] = []

        with self._lock, self.conn.transaction(), self.conn.cursor() as cur:
            # 문서 직렬화(교차 프로세스). 트랜잭션 종료 시 자동 해제.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (doc_id,))

            # 타입 불변식. prev status·supersedes 는 **frontmatter jsonb** 에서 읽는다
            # (supersedes 다중값 리스트를 온전히 비교 — 단일 컬럼 flatten 시 supersede 오분류).
            cur.execute("SELECT type, frontmatter FROM documents WHERE id=%s", (doc_id,))
            prev = cur.fetchone()
            if prev and prev[0] and prev[0] != doc_type:
                raise LintError(
                    [f"type 변경 불가: 기존 '{prev[0]}' → '{doc_type}' (문서 타입은 불변)"]
                )
            prev_fm = (prev[1] or {}) if prev else {}
            prev_status = prev_fm.get("status")
            prev_supersedes = prev_fm.get("supersedes")

            # 스냅샷 로드(+손상 감지).
            cur.execute("SELECT body, body_hash FROM snapshots WHERE doc_id=%s", (doc_id,))
            srow = cur.fetchone()
            old = None
            if srow is not None:
                if _sha(srow[0]) == srow[1]:
                    old = srow[0]
                else:
                    warnings.append(f"스냅샷 손상: {doc_id} → 전체 created 안전 처리")

            hunks = diffing.diff(old, nd.body)

            if change_type:
                ct = change_type
            else:
                ct = history_mod.determine_change_type(
                    snapshot_exists=(old is not None),
                    new_status=nd.frontmatter.get("status"),
                    prev_status=prev_status,
                    new_supersedes=nd.frontmatter.get("supersedes"),
                    prev_supersedes=prev_supersedes,
                )

            entries = history_mod.build(doc_id, hunks, actor=actor, change_type=ct, ts=now_ts)
            history_id = entries[0].ts if entries else None
            for e in entries:
                cur.execute(
                    "INSERT INTO history(doc_id, ts, actor, type, anchor, summary, "
                    "summary_source, delta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (doc_id, e.ts, e.actor, e.type, e.anchor, e.summary,
                     e.summary_source, e.delta),
                )

            fm = nd.frontmatter
            project = fm.get("project") or self.default_project
            body_hash = _sha(nd.body)
            cur.execute(
                """INSERT INTO documents
                     (id, type, status, title, project, tenant, source, author,
                      created, updated, tags, related, supersedes, body, raw,
                      body_hash, frontmatter)
                   VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT(id) DO UPDATE SET
                     type=excluded.type, status=excluded.status, title=excluded.title,
                     project=excluded.project, source=excluded.source, author=excluded.author,
                     created=excluded.created, updated=excluded.updated, tags=excluded.tags,
                     related=excluded.related, supersedes=excluded.supersedes,
                     body=excluded.body, raw=excluded.raw, body_hash=excluded.body_hash,
                     frontmatter=excluded.frontmatter""",
                (doc_id, doc_type, fm.get("status"), fm.get("title"), project,
                 fm.get("source"), fm.get("author"), str(fm.get("created", "")),
                 fm.get("updated"), json.dumps(fm.get("tags") or []),
                 json.dumps(fm.get("related") or []), _scalar(fm.get("supersedes")),
                 nd.body, nd.text, body_hash, json.dumps(fm, ensure_ascii=False)),
            )

            cur.execute(
                "INSERT INTO snapshots(doc_id, body, body_hash) VALUES (%s,%s,%s) "
                "ON CONFLICT(doc_id) DO UPDATE SET body=excluded.body, body_hash=excluded.body_hash",
                (doc_id, nd.body, body_hash),
            )

            if intended_diff:
                cur.execute(
                    "INSERT INTO docs_diff(doc_id, date, content) VALUES (%s,%s,%s) "
                    "ON CONFLICT(doc_id, date) DO UPDATE SET content=excluded.content",
                    (doc_id, now_ts[:10], intended_diff),
                )

            # chunks 재구성(앵커별 섹션 — tsv 는 생성 컬럼).
            cur.execute("DELETE FROM chunks WHERE doc_id=%s", (doc_id,))
            anchs = anchors_mod.parse_anchors(nd.body)
            if anchs:
                for a in anchs:
                    text = "\n".join(anchors_mod.section_lines(nd.body, a))
                    cur.execute(
                        "INSERT INTO chunks(doc_id, anchor, text) VALUES (%s,%s,%s)",
                        (doc_id, a.slug, text),
                    )
            elif nd.body.strip():
                cur.execute(
                    "INSERT INTO chunks(doc_id, anchor, text) VALUES (%s,'',%s)",
                    (doc_id, nd.body),
                )

        return SaveResult(
            id=doc_id, change_type=ct,
            anchors_changed=[h.anchor for h in hunks],
            history_id=history_id, warnings=warnings,
        )

    # ── 읽기 ──────────────────────────────────────────────────────────
    def get_raw(self, doc_id: str) -> str | None:
        with self._lock, self.conn.cursor() as cur:
            cur.execute("SELECT raw FROM documents WHERE id=%s", (doc_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def get_meta(self, doc_id: str) -> dict | None:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, type, status, title, source, updated, project, tenant, "
                "tags, body_hash FROM documents WHERE id=%s", (doc_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        m = dict(zip(_META_COLS, row[:8]))
        m["tags"] = row[8] or []
        m["body_hash"] = row[9]
        m["path"] = None  # PG 백엔드는 파일 경로 없음
        return m

    def exists(self, doc_id: str) -> bool:
        with self._lock, self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM documents WHERE id=%s", (doc_id,))
            return cur.fetchone() is not None

    def all_doc_ids(self) -> list[str]:
        with self._lock, self.conn.cursor() as cur:
            cur.execute("SELECT id FROM documents")
            return [r[0] for r in cur.fetchall()]

    def list_projects(self) -> list[str]:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT project FROM documents "
                "WHERE project IS NOT NULL ORDER BY project"
            )
            return [r[0] for r in cur.fetchall()]

    def _filter_sql(self, filters: dict, alias: str = "") -> tuple[str, list]:
        """필터 → WHERE 절 + 파라미터. tags 는 jsonb 포함(@>). (§2-6 필터 선행.)"""
        p = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list = []
        for key in _EQ_FILTERS:
            if filters.get(key) is not None:
                clauses.append(f"{p}{key}=%s")
                params.append(filters[key])
        tags = filters.get("tags")
        if tags:
            tags = [tags] if isinstance(tags, str) else tags
            clauses.append(f"{p}tags @> %s::jsonb")
            params.append(json.dumps(list(tags)))
        return (" AND ".join(clauses), params)

    def search(
        self, tokens: list[str], filters: dict | None = None, k: int = 10,
        *, mode: str = "and",
    ) -> list[Hit]:
        if not tokens:
            return []
        op = " & " if mode == "and" else " | "
        tsquery = op.join(tokens)  # 토큰은 \w+ 라 tsquery 안전
        filters = filters or {}
        where, params = self._filter_sql(filters, alias="d")
        sql = (
            "SELECT c.doc_id, c.anchor, c.text, ts_rank_cd(c.tsv, q) AS score "
            "FROM chunks c JOIN documents d ON d.id=c.doc_id, "
            "to_tsquery('simple', %s) q "
            "WHERE c.tsv @@ q"
        )
        args: list = [tsquery]
        if where:
            sql += " AND " + where
            args += params
        sql += " ORDER BY score DESC LIMIT %s"
        args.append(k)
        with self._lock, self.conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [Hit(doc_id=r[0], anchor=r[1], text=r[2], score=float(r[3])) for r in rows]

    def list_documents(
        self, filters: dict | None = None, *, limit: int | None = None, offset: int = 0,
    ) -> list[dict]:
        filters = filters or {}
        where, params = self._filter_sql(filters)
        sql = ("SELECT id, type, status, title, source, updated, project, tenant, tags "
               "FROM documents")
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params += [limit, offset]
        elif offset:
            sql += " OFFSET %s"
            params.append(offset)
        with self._lock, self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        out = []
        for r in rows:
            m = dict(zip(_META_COLS, r[:8]))
            m["tags"] = r[8] or []
            m["path"] = None
            out.append(m)
        return out

    def read_history(self, doc_id: str) -> list[dict]:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "SELECT ts, actor, type, anchor, summary, summary_source, delta "
                "FROM history WHERE doc_id=%s ORDER BY seq", (doc_id,),
            )
            rows = cur.fetchall()
        keys = ("ts", "actor", "type", "anchor", "summary", "summary_source", "delta")
        return [dict(zip(keys, r)) for r in rows]

    def read_docs_diff(self, doc_id: str, date: str | None = None) -> list[dict]:
        with self._lock, self.conn.cursor() as cur:
            if date is not None:
                cur.execute(
                    "SELECT date, content FROM docs_diff WHERE doc_id=%s AND date=%s",
                    (doc_id, date),
                )
            else:
                cur.execute(
                    "SELECT date, content FROM docs_diff WHERE doc_id=%s ORDER BY date",
                    (doc_id,),
                )
            return [{"date": r[0], "content": r[1]} for r in cur.fetchall()]

    def all_frontmatter(self) -> dict[str, dict]:
        with self._lock, self.conn.cursor() as cur:
            cur.execute("SELECT id, frontmatter FROM documents")
            return {r[0]: (r[1] or {}) for r in cur.fetchall()}

    # ── 제출 ──────────────────────────────────────────────────────────
    def create_submission(
        self, *, op: str, doc_id: str, raw_markdown: str, intended_diff: str | None,
        change_type: str | None, project: str | None, actor: str, prelint: dict, now: str,
        base_hash: str | None = None,
    ) -> dict:
        sub = {
            "id": _subs.new_id(now), "op": op, "doc_id": doc_id,
            "raw_markdown": raw_markdown, "intended_diff": intended_diff,
            "change_type": change_type, "project": project, "base_hash": base_hash,
            "actor": actor, "status": "pending", "prelint": prelint, "created": now,
            "reviewer": None, "reviewed_at": None, "note": None,
        }
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO submissions(id, op, doc_id, raw_markdown, intended_diff, "
                "change_type, project, base_hash, actor, status, prelint, created, reviewer, "
                "reviewed_at, note) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)",
                (sub["id"], op, doc_id, raw_markdown, intended_diff, change_type,
                 project, base_hash, actor, "pending", json.dumps(prelint, ensure_ascii=False),
                 now, None, None, None),
            )
        return sub

    def _sub_row(self, cur) -> list[dict]:
        cols = ("id", "op", "doc_id", "raw_markdown", "intended_diff", "change_type",
                "project", "base_hash", "actor", "status", "prelint", "created", "reviewer",
                "reviewed_at", "note")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    _SUB_SELECT = (
        "SELECT id, op, doc_id, raw_markdown, intended_diff, change_type, "
        "project, base_hash, actor, status, prelint, created, reviewer, reviewed_at, note "
        "FROM submissions"
    )

    def read_submission(self, sub_id: str) -> dict | None:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(self._SUB_SELECT + " WHERE id=%s", (sub_id,))
            rows = self._sub_row(cur)
        return rows[0] if rows else None

    def list_submissions(self, status: str | None = None) -> list[dict]:
        with self._lock, self.conn.cursor() as cur:
            sql = self._SUB_SELECT
            params: list = []
            if status is not None:
                sql += " WHERE status=%s"
                params.append(status)
            sql += " ORDER BY created DESC"
            cur.execute(sql, params)
            return self._sub_row(cur)

    def set_submission_status(
        self, sub_id: str, *, status: str, reviewer: str, note: str | None, now: str,
    ) -> dict:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "UPDATE submissions SET status=%s, reviewer=%s, reviewed_at=%s, note=%s "
                "WHERE id=%s", (status, reviewer, now, note, sub_id),
            )
        sub = self.read_submission(sub_id)
        if sub is None:
            raise KeyError(f"제출 없음: {sub_id}")
        return sub

    # ── 락 (advisory + 프로세스 내 RLock) ─────────────────────────────
    @contextmanager
    def _advisory(self, key: int):
        self._lock.acquire()  # 프로세스 내 직렬화(재진입) — reflect 도 이 RLock 사용
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (key,))  # 교차 프로세스
            try:
                yield
            finally:
                with self.conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            self._lock.release()

    def ingest_lock(self):
        return self._advisory(_INGEST_KEY)

    def submissions_lock(self):
        return self._advisory(_SUBMISSIONS_KEY)

    # ── 이관(import) 전용 헬퍼 — 파일 백엔드의 원본 이력/의도변경을 그대로 복사 ──
    def precreate_ids(self, doc_ids: list[str]) -> None:
        """id-only 행을 미리 넣어 이관 중 exists() 가 모든 문서에 대해 참이 되게 한다.

        reflect 의 lint 는 related/supersedes dangling 을 exists 로 검사하므로, 순서·순환에
        무관하게 이관이 성공하려면 문서 삽입 전 모든 id 가 존재해야 한다(반영 시 ON CONFLICT 로
        전체 컬럼이 덮어써진다). 이미 있는 행은 건드리지 않는다."""
        with self._lock, self.conn.cursor() as cur:
            for doc_id in doc_ids:
                cur.execute(
                    "INSERT INTO documents(id) VALUES (%s) ON CONFLICT(id) DO NOTHING",
                    (doc_id,),
                )

    def replace_history(self, doc_id: str, entries: list[dict]) -> None:
        with self._lock, self.conn.cursor() as cur:
            cur.execute("DELETE FROM history WHERE doc_id=%s", (doc_id,))
            for e in entries:
                cur.execute(
                    "INSERT INTO history(doc_id, ts, actor, type, anchor, summary, "
                    "summary_source, delta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (doc_id, e.get("ts"), e.get("actor"), e.get("type"),
                     e.get("anchor", ""), e.get("summary", ""),
                     e.get("summary_source", "rule"), e.get("delta", "")),
                )

    def put_docs_diff(self, doc_id: str, date: str, content: str) -> None:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO docs_diff(doc_id, date, content) VALUES (%s,%s,%s) "
                "ON CONFLICT(doc_id, date) DO UPDATE SET content=excluded.content",
                (doc_id, date, content),
            )

    def import_submission(self, sub: dict) -> None:
        """제출을 **원본 id·상태·검토자 그대로** upsert(이관 전용). 재실행 멱등(중복 생성 없음)."""
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO submissions(id, op, doc_id, raw_markdown, intended_diff, "
                "change_type, project, base_hash, actor, status, prelint, created, reviewer, "
                "reviewed_at, note) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                "reviewer=excluded.reviewer, reviewed_at=excluded.reviewed_at, "
                "note=excluded.note",
                (sub["id"], sub.get("op"), sub.get("doc_id"), sub.get("raw_markdown"),
                 sub.get("intended_diff"), sub.get("change_type"), sub.get("project"),
                 sub.get("base_hash"), sub.get("actor"), sub.get("status", "pending"),
                 json.dumps(sub.get("prelint") or {"ok": True, "reasons": []}, ensure_ascii=False),
                 sub.get("created"), sub.get("reviewer"), sub.get("reviewed_at"), sub.get("note")),
            )


def _scalar(v):
    """supersedes 가 리스트로 오면 첫 값만 컬럼에 저장(계보는 frontmatter jsonb 로도 보존)."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v
