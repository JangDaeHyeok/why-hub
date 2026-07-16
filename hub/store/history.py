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


def frontmatter_delta(prev_fm: dict | None, new_fm: dict | None) -> str:
    """이전/현재 frontmatter 의 변경 필드를 git-diff 스타일 `-`/`+` 로 직렬화.

    `updated` 는 매 저장마다 바뀌므로 제외한다. 본문 변경 없는 메타 전이(폐기/상태/제목/태그)의
    'delta(무엇)' 근거로 이력에 남긴다."""
    prev = prev_fm or {}
    new = new_fm or {}
    lines: list[str] = []
    for k in sorted((set(prev) | set(new)) - {"updated"}):
        pv, nv = prev.get(k), new.get(k)
        if pv == nv:
            continue
        if k in prev:
            lines.append(f"- {k}: {pv}")
        if k in new:
            lines.append(f"+ {k}: {nv}")
    return "\n".join(lines)


def build(
    doc_id: str,
    hunks: list[DiffHunk],
    *,
    actor: str,
    change_type: str,
    ts: str,
    meta_delta: str | None = None,
    summary_source: str = "rule",
) -> list[HistoryEntry]:
    """DiffHunk 목록 → HistoryEntry 목록. created 는 문서 단위 1항목, 본문무변경은 메타 1항목."""
    # 전체 추가(최초 저장/손상 복구/인제스천 신규) → 문서 단위 1항목.
    # 판별은 **diff 의 created 플래그(권위 신호)만** 사용한다 — 클라이언트가 change_type="created"
    # 를 위조해 편집을 '생성'으로 뭉개(멀티훅→첫훅) 이력을 왜곡하는 것을 막는다.
    if hunks and hunks[0].created:
        h = hunks[0]
        added = h.added
        entry_type = change_type or "created"  # ingest 신규면 type=ingest 로 프로버넌스 보존
        label = {"created": "문서 생성", "ingest": "인제스천"}.get(entry_type, entry_type)
        return [
            HistoryEntry(
                ts=ts,
                actor=actor,
                type=entry_type,
                anchor=h.anchor,
                summary=f"{label} ({len(added)}개 줄)",
                summary_source=summary_source,
                delta="\n".join(f"+ {line}" for line in added),
            )
        ]

    if hunks:
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

    # 본문 변경 없음(frontmatter-only) — 폐기/상태/제목/태그 등 메타 전이도 이력에 남긴다.
    # 단, **실제 frontmatter 변경이 있을 때만**(meta_delta 비어있지 않음) — 동일 내용 재저장 같은
    # 순수 no-op 은 기록하지 않는다(updated 만 바뀌는 것은 meta_delta 에서 제외됨).
    # (신규는 위 created 분기가 처리하므로 여기 오는 'created' 는 실질 신규가 아님 → 제외.)
    if meta_delta and change_type != "created":
        label = {
            "deprecation": "폐기 처리",
            "supersede": "상위 문서로 대체",
            "ingest": "인제스천(메타 갱신)",
        }.get(change_type, "메타데이터 변경")
        return [
            HistoryEntry(
                ts=ts,
                actor=actor,
                type=change_type,
                anchor="",
                summary=label,
                summary_source=summary_source,
                delta=meta_delta or "",
            )
        ]
    return []


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
