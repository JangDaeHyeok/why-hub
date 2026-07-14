"""P02 검증 — 정규화·lint·앵커.

수용 기준 ([Δ] §9 해당 항목 + 구현스펙-M1-M4-Phase.md P02):
- 정규화 멱등성 / frontmatter 재직렬화 안정 / 의미 보존
- lint: 필수 필드·enum·id 형식 위반 → LintError (부작용 없음)
- dangling related/supersedes → LintError
- ADR 대안 섹션 비어 있음 → LintError
- 앵커: 중복 헤더 slug 유일화(`__2`), 섹션 추출
"""

from __future__ import annotations

import pytest

from hub.config import Config
from hub.store import anchors as anchors_mod
from hub.store.lint import LintError, lint
from hub.store.normalize import NormalizedDoc, normalize


# ── 샘플 ──────────────────────────────────────────────────────────────
VALID_ADR = """---
id: adr-0007
type: adr
title: 인증 방식으로 JWT 대신 세션 채택
status: accepted
created: 2026-06-01
tags: [auth, security]
---

# 배경

기존 JWT 기반 인증은 만료·폐기 처리가 어렵다.

# 결정

서버 세션 + Redis 로 전환한다.

# 근거

즉시 폐기가 가능하고 운영이 단순하다.

# 대안

JWT 유지(만료 15분) 안을 검토했으나 폐기 지연 문제로 기각.

# 결과

로그인 흐름이 서버 세션에 의존하게 된다.
"""


def _nd(raw: str, now: str | None = None) -> NormalizedDoc:
    return normalize(raw, now=now)


# ── 정규화 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    [
        VALID_ADR,
        "---\nid: adr-0001\ntype: adr\ntitle: t\nstatus: proposed\ncreated: 2026-01-01\n---\n\n#배경\n\n\n\n내용   \n",
        "# 제목\r\n\r\n본문\r\n",  # CRLF
        "##머리\ntext\n#머리\nmore\n",  # 공백 없는 header 는 header 아님
    ],
)
def test_normalize_idempotent(raw):
    once = normalize(raw).text
    twice = normalize(once).text
    assert once == twice  # 바이트 동일


def test_frontmatter_reserialized_in_canonical_order():
    # 입력 순서가 뒤죽박죽이어도 항상 같은 순서로 재직렬화.
    raw = (
        "---\n"
        "status: accepted\n"
        "title: T\n"
        "created: 2026-01-01\n"
        "type: guide\n"
        "id: guide-0001\n"
        "tags: [b, a]\n"
        "---\n\n# H\n\nbody\n"
    )
    text = normalize(raw).text
    fm = text.split("---\n")[1]
    order = [ln.split(":")[0] for ln in fm.strip().splitlines() if ":" in ln and not ln.startswith(" ") and not ln.startswith("-")]
    # id, type, title, status 가 이 상대 순서로 앞서야 한다.
    assert order.index("id") < order.index("type") < order.index("title") < order.index("status")
    assert order.index("status") < order.index("tags")


def test_normalize_preserves_meaning():
    nd = normalize(VALID_ADR)
    # 문장(의미)은 그대로 남아야 한다 — 형식만 손댄다.
    assert "서버 세션 + Redis 로 전환한다." in nd.body
    assert "JWT 유지(만료 15분) 안을 검토했으나 폐기 지연 문제로 기각." in nd.body


def test_normalize_updated_set_when_now_given():
    nd = normalize(VALID_ADR, now="2026-07-14T09:00:00")
    assert nd.frontmatter["updated"] == "2026-07-14T09:00:00"
    # now 미지정 시 updated 를 임의로 만들지 않는다.
    assert "updated" not in normalize(VALID_ADR).frontmatter


def test_normalize_header_spacing_and_blank_lines():
    nd = normalize(
        "---\nid: guide-0001\ntype: guide\ntitle: t\nstatus: proposed\ncreated: 2026-01-01\n---\n#머리없음\n##  결정\n바로 내용\n"
    )
    # '#머리없음' 은 공백이 없어 헤더가 아님 → 본문 문단.
    assert "#머리없음" in nd.body
    # '##  결정' → 공백 1개로 정규화 + 뒤에 빈 줄 삽입.
    assert "## 결정\n\n바로 내용" in nd.body


def test_normalize_preserves_code_fence():
    raw = (
        "---\nid: guide-0001\ntype: guide\ntitle: t\nstatus: proposed\ncreated: 2026-01-01\n---\n\n"
        "# H\n\n```python\n# 이건 헤더가 아니다\n\n\nx = 1\n```\n"
    )
    nd = normalize(raw)
    # 코드펜스 안의 빈 줄·'#' 는 보존.
    assert "# 이건 헤더가 아니다\n\n\nx = 1" in nd.body


# ── 앵커 ──────────────────────────────────────────────────────────────
def test_anchor_slug_uniquification():
    body = "# 결정\n\na\n\n# 결정\n\nb\n\n# 결정\n\nc\n"
    anchs = anchors_mod.parse_anchors(body)
    slugs = [a.slug for a in anchs]
    assert slugs == ["결정", "결정__2", "결정__3"]
    assert [a.occurrence for a in anchs] == [1, 2, 3]


def test_anchor_path_chain():
    body = "# 결정\n\nx\n\n## 대안\n\ny\n"
    anchs = anchors_mod.parse_anchors(body)
    by_slug = {a.slug: a for a in anchs}
    assert by_slug["대안"].path == "결정/대안"
    assert by_slug["결정"].path == "결정"


def test_anchor_line_range_and_section_extraction():
    body = "# A\n\nalpha\n\n# B\n\nbeta\n"
    anchs = anchors_mod.parse_anchors(body)
    a = next(x for x in anchs if x.slug == "A")
    assert anchors_mod.section_content(body, a).strip() == "alpha"
    # A 의 range 는 B 시작 직전까지.
    b = next(x for x in anchs if x.slug == "B")
    assert a.line_range[1] == b.line_range[0]


def test_anchor_ignores_headers_in_code_fence():
    body = "# 진짜\n\n```\n# 가짜\n```\n\ntext\n"
    anchs = anchors_mod.parse_anchors(body)
    assert [a.slug for a in anchs] == ["진짜"]


# ── lint: 스키마 ──────────────────────────────────────────────────────
def test_lint_valid_adr_passes():
    lint(normalize(VALID_ADR), Config())  # 예외 없음


def test_lint_missing_required_field():
    raw = VALID_ADR.replace("status: accepted\n", "")
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config())
    assert any("status" in r for r in ei.value.reasons)


def test_lint_enum_violation():
    raw = VALID_ADR.replace("status: accepted", "status: bogus")
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config())
    assert any("status" in r for r in ei.value.reasons)


def test_lint_id_format_violation():
    raw = VALID_ADR.replace("id: adr-0007", "id: ADR_7")
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config())
    assert any("id" in r for r in ei.value.reasons)


def test_lint_dangling_reference():
    raw = VALID_ADR.replace(
        "tags: [auth, security]\n", "tags: [auth]\nrelated: [adr-9999]\n"
    )
    # adr-0007 만 존재하는 저장소를 흉내낸 exists_fn.
    exists = lambda i: i == "adr-0007"
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config(), exists_fn=exists)
    assert any("9999" in r or "dangling" in r for r in ei.value.reasons)


def test_lint_dangling_ok_when_target_exists():
    raw = VALID_ADR.replace(
        "tags: [auth, security]\n", "tags: [auth]\nrelated: [adr-0003]\n"
    )
    exists = lambda i: i in {"adr-0007", "adr-0003"}
    lint(normalize(raw), Config(), exists_fn=exists)  # 통과


# ── lint: ADR 필수 섹션 ───────────────────────────────────────────────
def test_lint_adr_empty_alternatives_rejected():
    raw = VALID_ADR.replace(
        "# 대안\n\nJWT 유지(만료 15분) 안을 검토했으나 폐기 지연 문제로 기각.\n",
        "# 대안\n\n",
    )
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config())
    assert any("대안" in r for r in ei.value.reasons)


def test_lint_adr_placeholder_alternatives_rejected():
    raw = VALID_ADR.replace(
        "JWT 유지(만료 15분) 안을 검토했으나 폐기 지연 문제로 기각.", "TODO"
    )
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config())
    assert any("대안" in r for r in ei.value.reasons)


def test_lint_adr_missing_section_rejected():
    raw = VALID_ADR.replace(
        "# 결과\n\n로그인 흐름이 서버 세션에 의존하게 된다.\n", ""
    )
    with pytest.raises(LintError) as ei:
        lint(normalize(raw), Config())
    assert any("결과" in r for r in ei.value.reasons)


def test_lint_adr_section_alias_accepted():
    # "결과" 대신 별칭 "결과 및 영향" 을 써도 통과.
    raw = VALID_ADR.replace("# 결과", "# 결과 및 영향")
    lint(normalize(raw), Config())


def test_lint_non_adr_skips_section_rules():
    raw = (
        "---\nid: guide-0001\ntype: guide\ntitle: t\nstatus: proposed\ncreated: 2026-01-01\n---\n\n"
        "# 아무거나\n\n내용\n"
    )
    lint(normalize(raw), Config())  # guide 는 필수 섹션 강제 없음
