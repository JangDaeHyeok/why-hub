"""lint 게이트 — 스키마·정규화 테스트·ADR 필수 섹션 ([Δ] §4).

`lint(doc) -> None`(통과) 또는 `raise LintError(reasons)`.
**부작용 이전**에 돈다(save §2 step2). 실패 사유는 사람이 읽을 수 있게 반환 → UI 표시.

id 유일성/dangling 참조는 인덱스가 필요하므로 `exists_fn` 콜백으로 주입한다
(P05 에서 FTS 조회로 연결). 미주입 시 존재성 검사(dangling)는 스킵한다.

구현 Phase: P02.
"""

from __future__ import annotations

import re
from typing import Callable

from ..config import Config
from ..models import DOC_STATUSES, DOC_TYPES
from . import anchors as anchors_mod
from .normalize import NormalizedDoc, normalize

REQUIRED_FM = ("id", "type", "title", "status", "created")

# ADR 필수 섹션에서 "비어 있음"으로 간주할 플레이스홀더.
_PLACEHOLDERS = {"", "tbd", "todo", "tba", "n/a", "na", "-", "—", "없음",
                 "작성", "작성예정", "미정"}


class LintError(Exception):
    """lint 게이트 실패. `reasons` 는 사람이 읽을 수 있는 사유 목록."""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


def lint(
    doc: NormalizedDoc,
    config: Config | None = None,
    *,
    exists_fn: Callable[[str], bool] | None = None,
) -> None:
    config = config or Config()
    fm = doc.frontmatter or {}
    reasons: list[str] = []

    reasons += _lint_schema(fm, config, exists_fn)
    anchs, structure_reasons = _lint_structure(doc)
    reasons += structure_reasons

    if fm.get("type") == "adr":
        reasons += _lint_adr_sections(doc, anchs, config)

    if reasons:
        raise LintError(reasons)


# ── (1) 스키마 ────────────────────────────────────────────────────────
def _lint_schema(fm, config, exists_fn) -> list[str]:
    reasons: list[str] = []

    for k in REQUIRED_FM:
        if not fm.get(k):
            reasons.append(f"필수 frontmatter 누락: {k}")

    dtype = fm.get("type")
    if dtype is not None and dtype not in DOC_TYPES:
        reasons.append(f"type enum 위반: {dtype}")

    status = fm.get("status")
    if status is not None and status not in DOC_STATUSES:
        reasons.append(f"status enum 위반: {status}")

    doc_id = fm.get("id")
    if doc_id is not None:
        # 경로 안전성: id 는 문서/스냅샷/이력/락/저널 경로에 그대로 삽입되므로,
        # 설정 정규식과 **무관하게** 경로 구분자·상위경로·제어문자를 금지한다(traversal 차단).
        sid = str(doc_id)
        if (
            "/" in sid or "\\" in sid or ".." in sid
            or sid.startswith(".") or "\x00" in sid or sid.strip() != sid or not sid
        ):
            reasons.append(f"id 에 경로 구분자/상위경로/제어문자 불가: {doc_id!r}")
        elif dtype in DOC_TYPES:
            pat = config.id_pattern(dtype)
            if not re.match(pat, sid):
                reasons.append(f"id 형식 위반: {doc_id} (기대: {pat})")

    # dangling related/supersedes — 존재성 오라클(exists_fn) 필요.
    if exists_fn is not None:
        targets: list[str] = []
        rel = fm.get("related") or []
        if isinstance(rel, list):
            targets += [str(x) for x in rel]
        sup = fm.get("supersedes")
        if sup:
            targets += [str(x) for x in (sup if isinstance(sup, list) else [sup])]
        for t in targets:
            if t == str(doc_id):
                continue
            if not exists_fn(t):
                reasons.append(f"dangling 참조: {t}")

    return reasons


# ── (2) 정규화/구조 ───────────────────────────────────────────────────
def _lint_structure(doc: NormalizedDoc):
    reasons: list[str] = []

    # 멱등성: 이미 정규화된 본문을 재정규화해도 불변이어야 한다.
    if normalize(doc.text).text != doc.text:
        reasons.append("정규화 멱등성 위반(본문이 정규화되지 않음)")

    try:
        anchs = anchors_mod.parse_anchors(doc.body)
    except Exception as e:  # 앵커 파싱 자체 실패 = 포맷 유효성 위반
        return [], [f"앵커 파싱 실패: {e}"]

    slugs = [a.slug for a in anchs]
    if len(slugs) != len(set(slugs)):
        reasons.append("앵커 slug 유일성 위반")

    # 깨진 내부 링크(#슬러그)만 검사 — 자기 문서 내 앵커로 해석.
    slug_set = set(slugs)
    for frag in re.findall(r"\]\(#([^)]+)\)", doc.body):
        if frag not in slug_set:
            reasons.append(f"깨진 내부 링크: #{frag}")

    return anchs, reasons


# ── (3) ADR 필수 섹션 (INVARIANT CLAUDE.md §2-5) ──────────────────────
def _lint_adr_sections(doc, anchs, config) -> list[str]:
    reasons: list[str] = []

    # 별칭 → 표준명 역인덱스 (정규화된 이름으로 매칭).
    alias_to_std: dict[str, str] = {}
    for std, aliases in config.section_aliases.items():
        alias_to_std.setdefault(_norm_name(std), std)
        for a in aliases:
            alias_to_std[_norm_name(a)] = std

    present: dict[str, object] = {}
    for a in anchs:
        std = alias_to_std.get(_norm_name(a.text))
        if std and std not in present:
            present[std] = a

    for std in config.adr_required_sections:
        if std not in present:
            reasons.append(f"ADR 필수 섹션 없음: {std}")
            continue
        content = anchors_mod.section_content(doc.body, present[std])
        if _is_empty(content):
            reasons.append(f"ADR 필수 섹션 비어 있음: {std}")

    return reasons


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", "", str(s).strip().lower())


def _is_empty(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return True
    compact = re.sub(r"\s+", "", stripped.lower())
    return compact in {re.sub(r"\s+", "", p.lower()) for p in _PLACEHOLDERS}
