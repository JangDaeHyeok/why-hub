"""SQLite FTS5 색인·재색인·검색 (필터 선행 → MATCH → bm25) ([Δ] §7).

- 스키마: `documents`(메타/필터) + `chunks_fts`(앵커별 섹션 FTS5).
  기획안2의 `project`/`tenant` 컬럼은 **지금은 NULL 로 미리 열어둔다**(마이그레이션 대비).
- PRAGMA: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`.
- `reindex_doc`: `documents` upsert + 기존 `chunks_fts` doc_id 삭제 후 앵커별 섹션 재삽입 + `body_hash`.
- `search`: **① 필터 선행**(documents 에서 doc_id 후보) → **② MATCH + doc_id IN 후보 → bm25 정렬**.
  (CLAUDE.md §2-6: 필터 → 검색 순서 고정. 후처리 필터에 의존하지 않는다.)

구현 Phase: P04.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

from ..models import Document, Hit
from . import anchors as anchors_mod

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY, type TEXT, status TEXT, title TEXT, path TEXT,
  tags TEXT, source TEXT, updated TEXT,
  project TEXT, tenant TEXT,          -- MVP: NULL. 기획안2 에서 사용.
  body_hash TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  doc_id UNINDEXED, anchor UNINDEXED, text,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""

# documents 에서 필터 가능한 단순 등가 컬럼.
_EQ_FILTERS = ("type", "status", "project", "tenant", "source")


def _project_in_clause(filters: dict, clauses: list, params: list) -> None:
    """프로젝트 ACL 집합 필터(필터-선행 §2-6). 빈 리스트 → 0건(deny-by-default)."""
    allowed = filters.get("project__in")
    if allowed is None:
        return
    allowed = list(allowed)
    if not allowed:
        clauses.append("1=0")
        return
    placeholders = ",".join("?" for _ in allowed)
    clauses.append(f"project IN ({placeholders})")
    params.extend(allowed)


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Index:
    """FTS5 인덱스 핸들. 문서 단위 경쟁은 파일 락(P05)이 막고, FTS 쓰기는 SQLite 가 직렬화."""

    def __init__(self, db_path, *, default_project: str | None = None):
        from pathlib import Path

        # 미지정(NULL) project 를 이 값으로 취급(멀티프로젝트). 쓰기 coercion·기존 행 보정에 사용.
        self.default_project = default_project
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        # 단일 커넥션을 여러 요청 스레드가 공유하므로(FastAPI 동기 핸들러 스레드풀),
        # 모든 DB 접근을 이 락으로 직렬화한다. "cannot start a transaction within a
        # transaction" 및 커밋 간섭을 막는다(문서 파일락은 동일 문서만 막음).
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")  # 동시 save 시 일시적 락 대기
        self.conn.executescript(_SCHEMA)
        # 마이그레이션: 기존 NULL project 행을 기본 프로젝트로 보정(멱등 — 다음 open 시 0건).
        if self.default_project:
            with self._lock, self.conn:
                self.conn.execute(
                    "UPDATE documents SET project=? WHERE project IS NULL",
                    (self.default_project,),
                )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── 색인 ──────────────────────────────────────────────────────────
    def reindex_doc(
        self, doc: Document, *, path: str | None = None, body_hash: str | None = None
    ) -> None:
        body = doc.body or ""
        if body_hash is None:
            body_hash = _sha(body)
        anchs = anchors_mod.parse_anchors(body)
        tags_json = json.dumps(doc.tags or [], ensure_ascii=False)

        with self._lock, self.conn:  # 스레드 직렬화 + 트랜잭션
            project = doc.project or self.default_project
            self.conn.execute(
                """INSERT INTO documents
                     (id, type, status, title, path, tags, source, updated,
                      project, tenant, body_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     type=excluded.type, status=excluded.status, title=excluded.title,
                     path=excluded.path, tags=excluded.tags, source=excluded.source,
                     updated=excluded.updated, project=excluded.project,
                     body_hash=excluded.body_hash""",
                (doc.id, doc.type, doc.status, doc.title, path, tags_json,
                 doc.source, doc.updated, project, body_hash),
            )
            # 기존 섹션 삭제 후 앵커별 재삽입 (중복 방지).
            self.conn.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc.id,))
            if anchs:
                for a in anchs:
                    text = "\n".join(anchors_mod.section_lines(body, a))
                    self.conn.execute(
                        "INSERT INTO chunks_fts(doc_id, anchor, text) VALUES (?, ?, ?)",
                        (doc.id, a.slug, text),
                    )
            elif body.strip():
                # 헤더가 없는 문서 → 전체를 단일 청크로.
                self.conn.execute(
                    "INSERT INTO chunks_fts(doc_id, anchor, text) VALUES (?, '', ?)",
                    (doc.id, body),
                )

    def remove_doc(self, doc_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self.conn.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))

    # ── 검색 (필터 선행) ──────────────────────────────────────────────
    def search(self, query: str, filters: dict | None = None, k: int = 10) -> list[Hit]:
        filters = filters or {}
        with self._lock:
            # ① 필터 선행 — documents 에서 doc_id 후보 집합.
            candidates = self._candidate_ids(filters)
            if not candidates:
                return []

            # ② MATCH + doc_id IN 후보 → bm25 정렬.
            placeholders = ",".join("?" * len(candidates))
            sql = (
                "SELECT doc_id, anchor, text, bm25(chunks_fts) AS score "
                "FROM chunks_fts "
                f"WHERE chunks_fts MATCH ? AND doc_id IN ({placeholders}) "
                "ORDER BY score LIMIT ?"
            )
            rows = self.conn.execute(sql, [query, *candidates, k]).fetchall()
        return [Hit(doc_id=r[0], anchor=r[1], text=r[2], score=r[3]) for r in rows]

    def _candidate_ids(self, filters: dict) -> list[str]:
        clauses: list[str] = []
        params: list = []
        for key in _EQ_FILTERS:
            if filters.get(key) is not None:
                clauses.append(f"{key}=?")
                params.append(filters[key])
        _project_in_clause(filters, clauses, params)
        # tags: 요청한 모든 태그를 포함(AND). JSON 배열에 대해 따옴표 포함 LIKE.
        tags = filters.get("tags")
        if tags:
            if isinstance(tags, str):
                tags = [tags]
            for t in tags:
                clauses.append("tags LIKE ?")
                params.append(f'%"{t}"%')
        where = " AND ".join(clauses)
        sql = "SELECT id FROM documents" + (f" WHERE {where}" if where else "")
        return [r[0] for r in self.conn.execute(sql, params)]

    # ── 조회 (exists_fn 연결 · reconcile 용) ──────────────────────────
    def exists(self, doc_id: str) -> bool:
        with self._lock:
            return (
                self.conn.execute(
                    "SELECT 1 FROM documents WHERE id=?", (doc_id,)
                ).fetchone()
                is not None
            )

    def body_hash(self, doc_id: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT body_hash FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
        return row[0] if row else None

    def all_doc_ids(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.conn.execute("SELECT id FROM documents")]

    def list_projects(self) -> list[str]:
        """인덱스에 존재하는 project 목록(중복 제거, NULL 제외) — UI 셀렉터용."""
        with self._lock:
            return [
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT project FROM documents "
                    "WHERE project IS NOT NULL ORDER BY project"
                )
            ]

    def list_documents(
        self, filters: dict | None = None, *, limit: int | None = None, offset: int = 0
    ) -> list[dict]:
        """메타데이터 목록 (필터 = 검색과 동일 규칙). 검색 없이 documents 만 조회."""
        filters = filters or {}
        clauses: list[str] = []
        params: list = []
        for key in _EQ_FILTERS:
            if filters.get(key) is not None:
                clauses.append(f"{key}=?")
                params.append(filters[key])
        _project_in_clause(filters, clauses, params)
        tags = filters.get("tags")
        if tags:
            if isinstance(tags, str):
                tags = [tags]
            for t in tags:
                clauses.append("tags LIKE ?")
                params.append(f'%"{t}"%')
        sql = (
            "SELECT id, type, status, title, path, tags, source, updated, "
            "project, tenant FROM documents"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        # offset 은 limit 없이도 적용되어야 한다(SQLite 는 OFFSET 에 LIMIT 필수 → -1 로 무제한).
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        with self._lock:
            cur = self.conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        out: list[dict] = []
        for row in rows:
            m = dict(zip(cols, row))
            m["tags"] = json.loads(m["tags"]) if m.get("tags") else []
            out.append(m)
        return out

    def get_meta(self, doc_id: str) -> dict | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, type, status, title, path, tags, source, updated, "
                "project, tenant, body_hash FROM documents WHERE id=?",
                (doc_id,),
            )
            row = cur.fetchone()
            cols = [c[0] for c in cur.description]
        if row is None:
            return None
        meta = dict(zip(cols, row))
        if meta.get("tags"):
            try:
                meta["tags"] = json.loads(meta["tags"])
            except (ValueError, TypeError):
                meta["tags"] = []
        else:
            meta["tags"] = []
        return meta


def open_index(root, *, default_project: str | None = None) -> Index:
    """저장소 루트의 index.sqlite 를 연다. default_project 지정 시 NULL project 를 그 값으로 취급."""
    from . import paths

    return Index(paths.index_path(root), default_project=default_project)
