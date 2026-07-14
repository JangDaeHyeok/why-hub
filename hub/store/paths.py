"""경로 규칙 — 지식 저장소 레이아웃 ([Δ] §1, 기획안1 §5.3).

**I/O 없음.** 오직 경로 조합만 한다(디렉토리 생성·읽기·쓰기는 각 store 모듈의 몫).

레이아웃::

    <root>/
      docs/<type>/<id>.md      # 현재 문서 (원천)
      .snapshots/<id>.md       # diff 기준점 (내부용)
      .snapshots/<id>.sha256   # 스냅샷 해시
      docs-diff/<id>.<date>.md # 의도된 변경 (스펙 선구동)
      history/<id>.history.md  # 자동 delta 이력 (append-only)
      index.sqlite             # FTS5 인덱스
      .locks/<id>.lock         # 문서별 파일 락
      .journal/<id>.json       # save 저널 (crash-safe)
"""

from __future__ import annotations

from pathlib import Path

# 저장소 하위 디렉토리 이름 (한 곳에서 관리)
DOCS_DIR = "docs"
SNAPSHOTS_DIR = ".snapshots"
DOCS_DIFF_DIR = "docs-diff"
HISTORY_DIR = "history"
LOCKS_DIR = ".locks"
JOURNAL_DIR = ".journal"
SUBMISSIONS_DIR = ".submissions"
INDEX_FILE = "index.sqlite"


def root(base: str | Path) -> Path:
    """저장소 루트를 Path 로 정규화한다."""
    return Path(base)


def docs_dir(base: str | Path) -> Path:
    return root(base) / DOCS_DIR


def doc_path(base: str | Path, doc_id: str, doc_type: str) -> Path:
    """현재 문서 경로: docs/<type>/<id>.md"""
    return docs_dir(base) / doc_type / f"{doc_id}.md"


def snapshots_dir(base: str | Path) -> Path:
    return root(base) / SNAPSHOTS_DIR


def snapshot_path(base: str | Path, doc_id: str) -> Path:
    """직전 스냅샷 경로: .snapshots/<id>.md"""
    return snapshots_dir(base) / f"{doc_id}.md"


def snapshot_hash_path(base: str | Path, doc_id: str) -> Path:
    """스냅샷 해시 경로: .snapshots/<id>.sha256"""
    return snapshots_dir(base) / f"{doc_id}.sha256"


def docs_diff_dir(base: str | Path) -> Path:
    return root(base) / DOCS_DIFF_DIR


def docs_diff_path(base: str | Path, doc_id: str, date: str) -> Path:
    """의도된 변경 경로: docs-diff/<id>.<date>.md"""
    return docs_diff_dir(base) / f"{doc_id}.{date}.md"


def history_dir(base: str | Path) -> Path:
    return root(base) / HISTORY_DIR


def history_path(base: str | Path, doc_id: str) -> Path:
    """이력 경로: history/<id>.history.md"""
    return history_dir(base) / f"{doc_id}.history.md"


def index_path(base: str | Path) -> Path:
    """FTS5 인덱스 파일 경로: index.sqlite"""
    return root(base) / INDEX_FILE


def locks_dir(base: str | Path) -> Path:
    return root(base) / LOCKS_DIR


def lock_path(base: str | Path, doc_id: str) -> Path:
    """문서별 락 경로: .locks/<id>.lock"""
    return locks_dir(base) / f"{doc_id}.lock"


def journal_dir(base: str | Path) -> Path:
    return root(base) / JOURNAL_DIR


def journal_path(base: str | Path, doc_id: str) -> Path:
    """save 저널 경로: .journal/<id>.json"""
    return journal_dir(base) / f"{doc_id}.json"


def submissions_dir(base: str | Path) -> Path:
    return root(base) / SUBMISSIONS_DIR


def submission_path(base: str | Path, sub_id: str) -> Path:
    """승인 대기 제출 경로: .submissions/<sub_id>.json (지식 콘텐츠 아님 — 임시 워크플로우 메타)."""
    return submissions_dir(base) / f"{sub_id}.json"


def all_dirs(base: str | Path) -> list[Path]:
    """저장소 초기화 시 만들어야 하는 디렉토리 목록 (I/O는 호출측)."""
    return [
        docs_dir(base),
        snapshots_dir(base),
        docs_diff_dir(base),
        history_dir(base),
        locks_dir(base),
        journal_dir(base),
    ]
