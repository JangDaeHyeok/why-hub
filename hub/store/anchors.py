"""앵커 — git diff 방식의 "섹션 식별" ([Δ] §5.1).

본문을 헤더 기준으로 파싱해 `(level, text, slug, path, occurrence, line_range)` 목록을 만든다.
diff(§5.2)는 각 hunk의 변경 줄을 `line_range` 에 대입해 **감싸는 최근접 헤더(앵커)** 를 찾는다.

규칙:
- `slug` = 헤더 텍스트 정규화(소문자화 안 함; 한글 유지, 공백→'-', 특수문자 제거).
- **유일성:** 같은 slug 가 여러 번이면 `slug`, `slug__2`, `slug__3` ….
- `path` = 상위 헤더 슬러그 체인(예: `결정/대안`). 참조는 slug(순번 포함)를 1급, path 는 보조.
- `line_range` = [start, end) (0-based, end 배타) — `body.split('\\n')` 인덱스 기준.
- 코드펜스(``` / ~~~) 안의 `#` 는 헤더로 보지 않는다.

구현 Phase: P02.
"""

from __future__ import annotations

import re

from ..models import Anchor

# 코드펜스 시작/종료 (백틱/틸드 3개 이상). 여는·닫는 것을 토글로 처리.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
# ATX 헤더: '#'*(1~6) 다음에 공백/탭이 오거나(텍스트 있음) 라인 끝(빈 헤더).
_HEADER_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*\S))?[ \t]*$")


def _slugify(text: str) -> str:
    """헤더 텍스트 → slug. 한글 유지, 공백→'-', 특수문자 제거."""
    s = text.strip()
    s = re.sub(r"\s+", "-", s)
    # 워드 문자(유니코드 letters/digits/'_')와 하이픈만 남긴다.
    s = re.sub(r"[^\w\-]", "", s)
    s = s.replace("_", "")  # '_' 는 유일성 접미사(__N)와 혼동되므로 제거
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "section"


def _header_lines(lines: list[str]) -> list[tuple[int, str, int]]:
    """코드펜스를 건너뛰며 헤더만 추출 → [(level, text, line_idx)]."""
    result: list[tuple[int, str, int]] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADER_RE.match(line)
        if m:
            level = len(m.group(1))
            text = (m.group(2) or "").strip()
            result.append((level, text, i))
    return result


def parse_anchors(body: str) -> list[Anchor]:
    """본문 → 앵커 목록. `line_range` 로 §5.2 diff 귀속이 이뤄진다."""
    lines = body.split("\n")
    headers = _header_lines(lines)

    anchors: list[Anchor] = []
    slug_counts: dict[str, int] = {}
    stack: list[tuple[int, str]] = []  # (level, path) — 조상 헤더 스택

    for idx, (level, text, line_idx) in enumerate(headers):
        end = headers[idx + 1][2] if idx + 1 < len(headers) else len(lines)

        base = _slugify(text)
        slug_counts[base] = slug_counts.get(base, 0) + 1
        occ = slug_counts[base]
        slug = base if occ == 1 else f"{base}__{occ}"

        # 자신보다 얕거나 같은 레벨은 조상이 아니므로 팝.
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_path = stack[-1][1] if stack else None
        path = f"{parent_path}/{slug}" if parent_path else slug
        stack.append((level, path))

        anchors.append(
            Anchor(
                level=level,
                text=text,
                slug=slug,
                path=path,
                occurrence=occ,
                line_range=(line_idx, end),
            )
        )
    return anchors


def section_lines(body: str, anchor: Anchor) -> list[str]:
    """앵커 범위의 전체 줄(헤더 라인 포함). FTS 색인·delta 에 사용."""
    lines = body.split("\n")
    start, end = anchor.line_range
    return lines[start:end]


def section_content(body: str, anchor: Anchor) -> str:
    """앵커의 본문(헤더 라인 제외, 하위 섹션 포함), 앞뒤 공백 제거."""
    lines = body.split("\n")
    start, end = anchor.line_range
    return "\n".join(lines[start + 1 : end]).strip()
