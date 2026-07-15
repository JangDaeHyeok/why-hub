"""Store 추상 — 서비스가 의존하는 영속 계층 인터페이스 (구현스펙-postgres-배포.md).

두 구현: FileStore(파일+SQLite FTS5, 기본/테스트), PostgresStore(배포). 서비스는 이 추상만 호출하고
백엔드별 dialect·영속 세부는 각 구현 안에 가둔다(CLAUDE.md §10). 순수 도메인(normalize/lint/anchors/
diffing/history.build)은 백엔드 무관하게 재사용된다.

`reflect` 가 유일한 쓰기 경로(§2-1) — normalize→lint→diff→history→snapshot→docs-diff→FTS 를 원자적으로 수행.
project 는 서비스가 미리 frontmatter 에 주입해 raw_markdown 으로 넘긴다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

from ..models import Hit, SaveResult


class Store(ABC):
    # ── 쓰기 (유일 진입점) ────────────────────────────────────────────
    @abstractmethod
    def reflect(
        self, raw_markdown: str, *, actor: str, change_type: str | None = None,
        intended_diff: str | None = None, now: str | None = None,
    ) -> SaveResult:
        """실제 반영. normalize→lint(부작용 전)→diff→이력→스냅샷→docs-diff→FTS 재색인을 원자적으로.
        lint 실패 시 LintError(아무것도 안 씀)."""

    # ── 읽기 ──────────────────────────────────────────────────────────
    @abstractmethod
    def get_raw(self, doc_id: str) -> str | None:
        """문서의 정규화된 원문(마크다운) 전체. 없으면 None. (문서 dict 조립은 서비스가.)"""

    @abstractmethod
    def get_meta(self, doc_id: str) -> dict | None:
        """문서 메타(id/type/status/title/path/tags/source/updated/project/tenant/body_hash). 없으면 None."""

    @abstractmethod
    def exists(self, doc_id: str) -> bool: ...

    @abstractmethod
    def all_doc_ids(self) -> list[str]: ...

    @abstractmethod
    def list_projects(self) -> list[str]: ...

    @abstractmethod
    def search(
        self, tokens: list[str], filters: dict | None = None, k: int = 10,
        *, mode: str = "and",
    ) -> list[Hit]:
        """필터-선행(§2-6) → FTS. tokens 는 dialect-중립(\\w+); 연산자 결합(AND|OR)은 백엔드가.
        빈 tokens → []."""

    @abstractmethod
    def list_documents(
        self, filters: dict | None = None, *, limit: int | None = None, offset: int = 0,
    ) -> list[dict]: ...

    @abstractmethod
    def read_history(self, doc_id: str) -> list[dict]:
        """이력 항목 dict 목록(시간순): ts/actor/type/anchor/summary/summary_source/delta."""

    @abstractmethod
    def read_docs_diff(self, doc_id: str, date: str | None = None) -> list[dict]:
        """의도된 변경 목록: [{date, content}]. date 지정 시 해당 날짜만."""

    @abstractmethod
    def all_frontmatter(self) -> dict[str, dict]:
        """모든 문서의 frontmatter dict(계보 계산용). {doc_id: frontmatter}."""

    # ── 제출(승인 큐) ─────────────────────────────────────────────────
    @abstractmethod
    def create_submission(
        self, *, op: str, doc_id: str, raw_markdown: str, intended_diff: str | None,
        change_type: str | None, project: str | None, actor: str, prelint: dict, now: str,
        base_hash: str | None = None,
    ) -> dict: ...

    @abstractmethod
    def read_submission(self, sub_id: str) -> dict | None: ...

    @abstractmethod
    def list_submissions(self, status: str | None = None) -> list[dict]: ...

    @abstractmethod
    def set_submission_status(
        self, sub_id: str, *, status: str, reviewer: str, note: str | None, now: str,
    ) -> dict: ...

    # ── 락 (동시성) ───────────────────────────────────────────────────
    @abstractmethod
    def ingest_lock(self) -> AbstractContextManager:
        """신규 ingest id 채번 직렬화."""

    @abstractmethod
    def submissions_lock(self) -> AbstractContextManager:
        """제출 상태 전이(approve/reject) 직렬화."""

    @abstractmethod
    def close(self) -> None: ...
