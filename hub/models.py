"""데이터 모델 — 순수 데이터 클래스 (부작용 없음).

[Δ] §1 (models.py), §5.1 (Anchor), §5.2 (DiffHunk), §5.3 (HistoryEntry),
§2 (SaveResult), §7 (Hit) 참조.

이 모듈은 저장소·인덱스·인터페이스 어디에도 의존하지 않는 값 객체만 담는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── frontmatter enum (기획안1 §5.1, [Δ] §4) ──────────────────────────────
DOC_TYPES = ("adr", "design-intent", "guide", "spec", "note", "reference")
DOC_STATUSES = ("proposed", "accepted", "deprecated", "superseded")

# 이력 항목의 change_type ([Δ] §5.3)
CHANGE_TYPES = ("created", "revision", "deprecation", "supersede", "ingest")


@dataclass
class Document:
    """정규화된 문서 하나 = frontmatter(메타) + 본문.

    frontmatter 필수: id, type, title, status, created ([Δ] §4).
    optional: tags, related, supersedes, source, author, updated.
    """

    id: str
    type: str
    title: str
    status: str
    created: str
    body: str = ""
    updated: str | None = None
    author: str | None = None
    source: str | None = None
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    supersedes: str | None = None


@dataclass
class Anchor:
    """섹션 식별자 — git diff 방식의 "감싸는 헤더" ([Δ] §5.1).

    slug 는 헤더 텍스트 정규화(한글 유지, 공백→'-', 특수문자 제거) + 유일화(`__2`).
    line_range 는 [start, end) (0-based, end 배타) — diff hunk 귀속에 사용.
    """

    level: int
    text: str
    slug: str
    path: str  # 상위 헤더 슬러그 체인 (예: "결정/대안"), 보조 식별자
    occurrence: int  # 동일 slug 발생 순번 (1-base)
    line_range: tuple[int, int]


@dataclass
class DiffHunk:
    """줄 단위 diff 조각을 감싸는 최근접 앵커에 귀속한 결과 ([Δ] §5.2).

    added/removed 는 git diff 스타일의 `+`/`-` 줄 (마커 없는 순수 텍스트).
    created 는 스냅샷이 없는 최초 저장(전체를 created 로 간주).
    """

    anchor: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    created: bool = False


@dataclass
class HistoryEntry:
    """append-only 이력 항목 — 앵커별 1항목 ([Δ] §5.3)."""

    ts: str
    actor: str
    type: str  # CHANGE_TYPES 중 하나
    anchor: str
    summary: str
    summary_source: str = "rule"  # rule | auto-llm
    delta: str = ""  # `+`/`-` 줄 (git diff 스타일)


@dataclass
class SaveResult:
    """save 루틴의 반환값 ([Δ] §2)."""

    id: str
    change_type: str
    anchors_changed: list[str] = field(default_factory=list)
    history_id: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Hit:
    """검색 결과 한 건 — 섹션(앵커) 단위 + bm25 점수 ([Δ] §7)."""

    doc_id: str
    anchor: str
    text: str
    score: float
