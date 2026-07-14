"""이력 — 앵커별 HistoryEntry 생성·append (append-only) ([Δ] §5.3).

- `build(...)` — 앵커별 1항목(created 는 문서 전체 1항목).
- `determine_change_type(...)` — 자동 판정(스냅샷 없음→created, accepted→deprecated→
  deprecation, supersedes 신규 등장→supersede, ingest 경유→ingest, 그 외→revision).
- `append(...)` — history/<id>.history.md 에 **append-only** 기록. 기존 항목 수정·삭제 금지.
- `delta` = git diff 스타일 `-`/`+` 줄. **`delta`(무엇)는 항상 정확**하므로 요약이 의심되면 근거.
- **규칙 기반 summary** 기본. LLM 요약은 `summary_source: auto-llm` 로 구분(옵션, 이 Phase 밖).

구현 Phase: P03.
"""

from __future__ import annotations

import yaml

from ..models import DiffHunk, HistoryEntry
from . import paths


def determine_change_type(
    *,
    snapshot_exists: bool,
    new_status: str | None = None,
    prev_status: str | None = None,
    new_supersedes=None,
    prev_supersedes=None,
    via_ingest: bool = False,
) -> str:
    """[Δ] §5.3 자동 판정. 인자로 명시 change_type 이 오면 save 가 그걸 존중(여기 호출 안 함)."""
    if not snapshot_exists:
        return "created"
    if prev_status == "accepted" and new_status == "deprecated":
        return "deprecation"
    if _newly_appeared(prev_supersedes, new_supersedes):
        return "supersede"
    if via_ingest:
        return "ingest"
    return "revision"


def _as_set(v) -> set[str]:
    if not v:
        return set()
    if isinstance(v, (list, tuple, set)):
        return {str(x) for x in v}
    return {str(v)}


def _newly_appeared(prev, new) -> bool:
    return bool(_as_set(new) - _as_set(prev))


def build(
    doc_id: str,
    hunks: list[DiffHunk],
    *,
    actor: str,
    change_type: str,
    ts: str,
    summary_source: str = "rule",
) -> list[HistoryEntry]:
    """DiffHunk 목록 → HistoryEntry 목록. created 는 문서 단위 1항목."""
    # 전체 추가(최초 저장/손상 복구/인제스천 신규) → 문서 단위 1항목.
    if change_type == "created" or (hunks and hunks[0].created):
        h = hunks[0] if hunks else None
        added = h.added if h else []
        entry_type = change_type or "created"  # ingest 신규면 type=ingest 로 프로버넌스 보존
        label = {"created": "문서 생성", "ingest": "인제스천"}.get(entry_type, entry_type)
        return [
            HistoryEntry(
                ts=ts,
                actor=actor,
                type=entry_type,
                anchor=(h.anchor if h else ""),
                summary=f"{label} ({len(added)}개 줄)",
                summary_source=summary_source,
                delta="\n".join(f"+ {line}" for line in added),
            )
        ]

    entries: list[HistoryEntry] = []
    for h in hunks:
        delta = "\n".join(
            [f"- {line}" for line in h.removed] + [f"+ {line}" for line in h.added]
        )
        where = f"'{h.anchor}' 섹션 " if h.anchor else ""
        summary = f"{where}변경 (+{len(h.added)}/-{len(h.removed)} 줄)"
        entries.append(
            HistoryEntry(
                ts=ts,
                actor=actor,
                type=change_type,
                anchor=h.anchor,
                summary=summary,
                summary_source=summary_source,
                delta=delta,
            )
        )
    return entries


def _entry_to_dict(e: HistoryEntry) -> dict:
    return {
        "ts": e.ts,
        "actor": e.actor,
        "type": e.type,
        "anchor": e.anchor,
        "summary": e.summary,
        "summary_source": e.summary_source,
        "delta": e.delta,
    }


def append(doc_id: str, entries: list[HistoryEntry], root) -> str | None:
    """history/<id>.history.md 에 append-only 로 기록. history_id(첫 항목 ts) 반환."""
    if not entries:
        return None
    path = paths.history_path(root, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    chunk = "".join(
        yaml.safe_dump(
            [_entry_to_dict(e)],
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        for e in entries
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(chunk)
    return entries[0].ts


def read(doc_id: str, root) -> list[HistoryEntry]:
    """이력 파일을 파싱해 HistoryEntry 목록으로 (get_history 용, P06)."""
    path = paths.history_path(root, doc_id)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [
        HistoryEntry(
            ts=d.get("ts"),
            actor=d.get("actor"),
            type=d.get("type"),
            anchor=d.get("anchor", ""),
            summary=d.get("summary", ""),
            summary_source=d.get("summary_source", "rule"),
            delta=d.get("delta", ""),
        )
        for d in data
    ]
