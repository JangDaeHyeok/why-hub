"""FileStore — 파일 저장소 + SQLite FTS5 인덱스 (기본 백엔드, 로컬/테스트).

기존 store 모듈(save/history/snapshots/index_fts/submissions/locking/paths)에 위임한다.
행동은 이전과 동일하며, 서비스가 `Store` 추상에만 의존하도록 감싸는 얇은 어댑터다.
FTS5 질의 dialect(따옴표 AND / OR)를 여기(search)에 가둔다.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..models import Hit, SaveResult
from . import history as history_mod
from . import journal
from . import paths
from . import reconcile
from . import save as save_store
from . import submissions as submissions_store
from .base import Store
from .index_fts import open_index
from .locking import doc_lock
from .normalize import normalize

_INGEST_LOCK_ID = "__ingest__"


class FileStore(Store):
    def __init__(self, root, config: Config):
        self.root = Path(root)
        self.config = config
        # open_index 가 파일을 생성하므로 존재 여부는 그 **이전** 에 캡처한다.
        index_existed = paths.index_path(self.root).exists()
        self.index = open_index(self.root, default_project=config.default_project)
        # 부분 save(pending 저널)뿐 아니라 **인덱스 자체 유실/불일치** 도 감지해 수렴시킨다.
        # index.sqlite 만 지우고 재기동하면 문서 파일은 남지만 목록/조회/검색이 전부 빈 결과가 되므로,
        # 인덱스가 새로 생성됐거나(파일 부재) docs/ 문서 수와 어긋나면 reconcile 로 재색인한다.
        if journal.pending(self.root) or not index_existed or self._index_stale():
            reconcile.run(self.root, self.config, index=self.index)

    def _index_stale(self) -> bool:
        """docs/ 의 문서 수와 인덱스 문서 수가 다르면 True (인덱스 유실·불일치 감지)."""
        docs_dir = paths.docs_dir(self.root)
        n_files = sum(1 for _ in docs_dir.glob("*/*.md")) if docs_dir.exists() else 0
        return n_files != len(self.index.all_doc_ids())

    def close(self) -> None:
        self.index.close()

    # ── 쓰기 ──────────────────────────────────────────────────────────
    def reflect(
        self, raw_markdown: str, *, actor: str, change_type: str | None = None,
        intended_diff: str | None = None, now: str | None = None,
    ) -> SaveResult:
        return save_store.save_document(
            raw_markdown, root=self.root, actor=actor, config=self.config,
            change_type=change_type, intended_diff=intended_diff,
            index=self.index, now=now,
        )

    # ── 읽기 ──────────────────────────────────────────────────────────
    def get_raw(self, doc_id: str) -> str | None:
        meta = self.index.get_meta(doc_id)
        if not meta or not meta.get("path"):
            return None
        p = self.root / meta["path"]
        return p.read_text(encoding="utf-8") if p.exists() else None

    def get_meta(self, doc_id: str) -> dict | None:
        return self.index.get_meta(doc_id)

    def exists(self, doc_id: str) -> bool:
        return self.index.exists(doc_id)

    def all_doc_ids(self) -> list[str]:
        return self.index.all_doc_ids()

    def list_projects(self) -> list[str]:
        return self.index.list_projects()

    def search(
        self, tokens: list[str], filters: dict | None = None, k: int = 10,
        *, mode: str = "and",
    ) -> list[Hit]:
        if not tokens:
            return []
        # FTS5 dialect: 각 토큰을 따옴표로 감싸 리터럴화하고(예약어 OR/AND/NOT/NEAR 무력화)
        # AND 는 공백 나열, OR 는 실제 OR 연산자로 결합한다.
        quoted = [f'"{t}"' for t in tokens]
        query = " ".join(quoted) if mode == "and" else " OR ".join(quoted)
        return self.index.search(query, filters, k)

    def list_documents(
        self, filters: dict | None = None, *, limit: int | None = None, offset: int = 0,
    ) -> list[dict]:
        return self.index.list_documents(filters, limit=limit, offset=offset)

    def read_history(self, doc_id: str) -> list[dict]:
        if not paths.is_safe_doc_id(doc_id):  # 경로 주입 방어(서비스 존재검사에 더한 심층방어)
            return []
        return [
            {
                "ts": e.ts, "actor": e.actor, "type": e.type, "anchor": e.anchor,
                "summary": e.summary, "summary_source": e.summary_source, "delta": e.delta,
            }
            for e in history_mod.read(doc_id, self.root)
        ]

    def read_docs_diff(self, doc_id: str, date: str | None = None) -> list[dict]:
        if not paths.is_safe_doc_id(doc_id):  # glob('<id>.*.md') 주입 방어(심층방어)
            return []
        d = paths.docs_diff_dir(self.root)
        if not d.exists():
            return []
        out: list[dict] = []
        prefix = f"{doc_id}."
        for f in sorted(d.glob(f"{doc_id}.*.md")):
            dt = f.name[len(prefix):-3]  # '<id>.' 와 '.md' 제거
            if date is not None and dt != date:
                continue
            out.append({"date": dt, "content": f.read_text(encoding="utf-8")})
        return out

    def all_frontmatter(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for meta in self.index.list_documents():
            doc_id = meta["id"]
            p = self.root / meta["path"] if meta.get("path") else None
            out[doc_id] = (
                normalize(p.read_text(encoding="utf-8")).frontmatter
                if p and p.exists() else {}
            )
        return out

    # ── 제출 ──────────────────────────────────────────────────────────
    def create_submission(
        self, *, op: str, doc_id: str, raw_markdown: str, intended_diff: str | None,
        change_type: str | None, project: str | None, actor: str, prelint: dict, now: str,
        base_hash: str | None = None,
    ) -> dict:
        return submissions_store.create(
            self.root, op=op, doc_id=doc_id, raw_markdown=raw_markdown,
            intended_diff=intended_diff, change_type=change_type, project=project,
            actor=actor, prelint=prelint, now=now, base_hash=base_hash,
        )

    def read_submission(self, sub_id: str) -> dict | None:
        return submissions_store.read(self.root, sub_id)

    def list_submissions(self, status: str | None = None) -> list[dict]:
        return submissions_store.list(self.root, status)

    def set_submission_status(
        self, sub_id: str, *, status: str, reviewer: str, note: str | None, now: str,
    ) -> dict:
        return submissions_store.set_status(
            self.root, sub_id, status=status, reviewer=reviewer, note=note, now=now,
        )

    # ── 락 ────────────────────────────────────────────────────────────
    def ingest_lock(self):
        return doc_lock(_INGEST_LOCK_ID, self.root, timeout=self.config.lock_timeout)

    def submissions_lock(self):
        return doc_lock(submissions_store.LOCK_ID, self.root, timeout=self.config.lock_timeout)
