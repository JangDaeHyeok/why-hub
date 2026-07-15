"""정규화 — 결정론·멱등. 형식만 손대고 의미 재작성 금지 ([Δ] §3).

계약:
- `normalize(raw, *, now=None) -> NormalizedDoc` 는 **결정론적**이다.
- **멱등성:** `normalize(normalize(x).text).text == normalize(x).text` (바이트 동일).
- frontmatter 필드는 **정해진 순서**로 재직렬화(멱등성 핵심). 누락 optional 은 넣지 않음.
- `updated` 는 `now` 가 주어질 때만 세팅(시계를 내부에서 읽지 않음 → 결정론 보장).
- 본문은 **형식만** 정규화(개행/헤더 표기/빈 줄/트레일링 공백). 의미 재작성 절대 금지.

구현 Phase: P02.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

import yaml

from .anchors import fence_closes, fence_open

# frontmatter 재직렬화 순서 (models.Document 필드 순서와 정렬).
_FM_ORDER = [
    "id", "type", "title", "status", "project", "tags", "related",
    "supersedes", "source", "author", "created", "updated",
]

_HEADER_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*\S))?[ \t]*$")


@dataclass
class NormalizedDoc:
    """정규화 결과 — frontmatter(dict) + 본문 + 전체 직렬화 텍스트."""

    frontmatter: dict
    body: str
    text: str

    @property
    def id(self) -> str | None:
        return self.frontmatter.get("id")

    @property
    def type(self) -> str | None:
        return self.frontmatter.get("type")


def normalize(raw: str, *, now: str | None = None) -> NormalizedDoc:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    fm, body = _split_frontmatter(raw)
    fm = _coerce(fm)
    if now is not None:
        fm["updated"] = now
    body_n = _normalize_body(body)
    fm_text = _dump_frontmatter(fm)
    text = fm_text + body_n
    return NormalizedDoc(frontmatter=fm, body=body_n, text=text)


# ── frontmatter ───────────────────────────────────────────────────────
def _split_frontmatter(raw: str) -> tuple[dict, str]:
    lines = raw.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                block = "\n".join(lines[1:i])
                body = "\n".join(lines[i + 1 :])
                fm = yaml.safe_load(block) if block.strip() else {}
                if not isinstance(fm, dict):
                    fm = {}
                return fm, body
        # 닫는 '---' 없음 → 전체를 본문으로 (frontmatter 없음).
    return {}, raw


def _dump_frontmatter(fm: dict) -> str:
    ordered: dict = {}
    for k in _FM_ORDER:
        if k in fm and fm[k] not in (None, [], ""):
            ordered[k] = fm[k]
    # 스키마에 없는 추가 키는 유실 방지 위해 이름순으로 뒤에 붙인다.
    for k in sorted(fm):
        if k not in _FM_ORDER and fm[k] not in (None, [], ""):
            ordered[k] = fm[k]
    if not ordered:
        return ""
    dumped = yaml.safe_dump(
        ordered, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return "---\n" + dumped + "---\n"


def _coerce(v):
    """YAML 이 date/datetime 으로 파싱한 값을 문자열로 되돌린다(타입 안정)."""
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


# ── 본문 ──────────────────────────────────────────────────────────────
def _normalize_body(body: str) -> str:
    lines = body.split("\n")

    # Pass A: 트레일링 공백 제거 + 헤더 표기 통일 (코드펜스 밖에서만).
    a: list[str] = []
    fence = None  # 열린 펜스의 (마커문자, 길이); None 이면 펜스 밖.
    for line in lines:
        if fence is not None:
            if fence_closes(line, fence):
                fence = None
                a.append(line.rstrip())
            else:
                a.append(line)  # 코드펜스 내부는 그대로 보존
            continue
        op = fence_open(line)
        if op is not None:
            fence = op
            a.append(line.rstrip())
            continue
        stripped = line.rstrip()
        m = _HEADER_RE.match(stripped)
        if m:
            level = len(m.group(1))
            text = (m.group(2) or "").strip()
            a.append("#" * level + (" " + text if text else ""))
        else:
            a.append(stripped)

    # Pass B: 빈 줄 규칙 (연속 빈 줄 ≤1, 헤더 앞뒤 빈 줄 1개, 코드펜스 밖에서만).
    out: list[str] = []
    fence = None
    for line in a:
        if fence is not None:
            out.append(line)
            if fence_closes(line, fence):
                fence = None
            continue
        op = fence_open(line)
        if op is not None:
            fence = op
            out.append(line)
            continue
        is_header = bool(_HEADER_RE.match(line)) and line.startswith("#")
        if line == "":
            if not out or out[-1] == "":
                continue  # 선행 빈 줄 제거 + 연속 빈 줄 축약
            out.append("")
        elif is_header:
            if out and out[-1] != "":
                out.append("")  # 헤더 앞 빈 줄 1개
            out.append(line)
            out.append("")  # 헤더 뒤 빈 줄 1개 (다음 줄이 빈 줄이면 축약됨)
        else:
            out.append(line)

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n" if out else ""
