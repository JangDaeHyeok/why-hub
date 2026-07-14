"""diff + 귀속 — 줄 단위 unified diff → hunk → 최근접 헤더(앵커) 귀속 ([Δ] §5.2).

- `diff(old, new) -> list[DiffHunk]`.
- `old is None` → 전체를 **created** (hunk 하나, added=전체 본문).
- 그 외: `difflib` 줄 단위 비교 → 변경 줄을 `new` 앵커 `line_range` 에 대입해
  **감싸는 최근접 헤더(앵커)** 에 귀속. 삭제만 있는 hunk 는 삭제 위치 직전 헤더에 귀속.
- **여러 앵커에 걸친 변경은 앵커별로 hunk 분할**(이력 항목이 앵커 단위로 나오도록).

구현 Phase: P03.
"""

from __future__ import annotations

import difflib

from ..models import DiffHunk
from . import anchors as anchors_mod


def diff(old: str | None, new: str) -> list[DiffHunk]:
    new_lines = new.split("\n")

    # 스냅샷 없음(또는 손상) → 전체 created.
    if old is None:
        anchs = anchors_mod.parse_anchors(new)
        first = anchs[0].slug if anchs else ""
        return [DiffHunk(anchor=first, added=list(new_lines), removed=[], created=True)]

    old_lines = old.split("\n")
    anchs = anchors_mod.parse_anchors(new)
    header_starts = {a.line_range[0] for a in anchs}

    def anchor_at(i: int) -> str:
        """new 의 i번째 줄을 감싸는 앵커 slug (없으면 '')."""
        for a in anchs:
            if a.line_range[0] <= i < a.line_range[1]:
                return a.slug
        return ""

    def anchor_for_delete(j: int) -> str:
        """삭제 위치(new 기준 j) 직전(=감싸는) 헤더 앵커."""
        if not anchs:
            return ""
        pos = j if j < len(new_lines) else len(new_lines) - 1
        # 삭제가 헤더 경계에 걸리면 직전 섹션에 귀속.
        if pos in header_starts and j > 0:
            pos = j - 1
        for a in anchs:
            if a.line_range[0] <= pos < a.line_range[1]:
                return a.slug
        return ""

    buckets: dict[str, DiffHunk] = {}
    order: list[str] = []

    def bucket(slug: str) -> DiffHunk:
        if slug not in buckets:
            buckets[slug] = DiffHunk(anchor=slug, added=[], removed=[])
            order.append(slug)
        return buckets[slug]

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("insert", "replace"):
            # 추가 줄은 각자의 new 인덱스로 귀속 → 여러 앵커에 걸치면 자연히 분할.
            for j in range(j1, j2):
                bucket(anchor_at(j)).added.append(new_lines[j])
        if tag in ("delete", "replace"):
            slug = anchor_for_delete(j1)
            b = bucket(slug)
            for i in range(i1, i2):
                b.removed.append(old_lines[i])

    return [buckets[s] for s in order]
