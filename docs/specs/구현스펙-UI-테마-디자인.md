# 구현 스펙 (초기 v0) — UI 테마 · 디자인 시스템

> **상태:** 초기 초안(v0). 색상 값·간격 스케일은 **디자인 토큰으로 고정**하되, 정확한 색상값은
> 스크린 검토로 미세조정한다(토큰 이름·구조는 계약, 값은 튜닝 대상).
> **범위:** 기획안 1 §9(브라우징 UI) · §9.4(경량 커스텀 프론트)의 **시각 레이어**.
> **불변식:** UI는 **HTTP API만 경유**한다(§9.3). 테마는 **표현(presentation) 전용**이며 코어·save·리트리버
> 로직에 어떤 영향도 주지 않는다.

---

## 1. 목적 · 원칙

읽기/브라우징(HTMX)과 편집/AI 생성(마크다운 에디터)이 **하나의 시각 언어**를 공유하도록 디자인 토큰을
정의한다. "What이 아니라 Why"라는 제품 철학에 맞춰 **조용하고 문서 중심적인** 화면을 지향한다.

- **라이트(화이트) · 다크(블랙) 두 테마를 모두 지원**한다. 어느 쪽도 부차적이지 않다(동등 1급).
- **기본 톤은 화이트/블랙 계열의 무채색**, **포인트(accent)는 블루 또는 퍼플 계열** 한 종류.
- 색으로 정보를 과하게 칠하지 않는다. 텍스트 가독성과 코드/마크다운 렌더 대비를 최우선한다.
- 접근성: 본문 텍스트 대비 **WCAG AA(4.5:1) 이상**, 큰 텍스트·UI 요소 **3:1 이상**.

## 2. 테마 계약 (하드 규칙)

1. 테마는 **`data-theme` 속성**으로 전환한다: `<html data-theme="light">` / `data-theme="dark"`.
2. 모든 색·간격·반경·그림자는 **CSS 커스텀 프로퍼티(디자인 토큰)** 로만 참조한다.
   컴포넌트 CSS에 **하드코딩 색상 리터럴 금지**(예: `#fff`, `rgb(...)` 직접 사용 금지 — 토큰 경유).
3. **의미 토큰(semantic) → 원시 토큰(primitive)** 2계층. 컴포넌트는 **의미 토큰만** 사용한다.
   (예: 컴포넌트는 `--bg-surface`를 쓰고, 그 값이 라이트/다크에서 각각 다른 원시값을 가리킨다.)
4. **포인트 색은 accent 토큰 한 계열로 통일.** 블루/퍼플 중 택1을 기본으로 하되, `--accent-*` 토큰만
   바꾸면 전체가 따라오도록 한다(§4.4).
5. 초기 테마는 **OS 설정(`prefers-color-scheme`) 추종**, 사용자가 토글하면 **`localStorage`에 저장**해 우선.
   FOUC 방지를 위해 테마 결정 스크립트는 `<head>`에서 **렌더 전 인라인 실행**.

## 3. 원시 토큰 (primitive) — 팔레트

무채색 스케일 + accent 스케일. 값은 v0 시작점(튜닝 대상).

### 3.1 무채색(gray) 스케일

```
--gray-0:   #ffffff
--gray-50:  #f7f8fa
--gray-100: #eef0f4
--gray-200: #e2e5ea
--gray-300: #cbd0d9
--gray-400: #9aa1ad
--gray-500: #6b727e
--gray-600: #4a505a
--gray-700: #333840
--gray-800: #20242b
--gray-900: #14171c
--gray-950: #0c0e12
--gray-1000: #08090c
```

### 3.2 accent — 블루 계열(기본 후보 A)

```
--blue-300: #7aa7ff
--blue-400: #4d86ff
--blue-500: #2f6bff   /* 기본 accent */
--blue-600: #1f54e0
--blue-700: #1a44b8
```

### 3.3 accent — 퍼플 계열(기본 후보 B)

```
--purple-300: #b39cff
--purple-400: #9873ff
--purple-500: #7c52f5   /* 기본 accent */
--purple-600: #6438da
--purple-700: #522eb0
```

### 3.4 상태색(state) — 무채색 위에서만 소량 사용

```
--green-500:  #2fa36b   /* 성공 / lint 통과 */
--amber-500:  #d9922b   /* 경고 / 확인 필요 */
--red-500:    #d64545   /* 오류 / lint 차단 */
```

## 4. 의미 토큰 (semantic) — 테마별 매핑

컴포넌트는 **아래 토큰만** 참조한다. 라이트/다크에서 원시값만 갈아끼운다.

### 4.1 라이트 테마 (`data-theme="light"`)

```
/* 배경 */
--bg-canvas:    var(--gray-50)    /* 페이지 바탕 */
--bg-surface:   var(--gray-0)     /* 카드·패널·에디터 */
--bg-subtle:    var(--gray-100)   /* 코드블록·인풋·hover */
--bg-inset:     var(--gray-200)   /* 눌린/선택 배경 */

/* 텍스트 */
--fg-default:   var(--gray-900)   /* 본문 */
--fg-muted:     var(--gray-500)   /* 보조·메타(actor/날짜) */
--fg-subtle:    var(--gray-400)   /* placeholder */
--fg-on-accent: var(--gray-0)     /* accent 위 텍스트 */

/* 경계·구분 */
--border-default: var(--gray-200)
--border-strong:  var(--gray-300)

/* 포인트 */
--accent-fg:    var(--accent-600) /* 링크·강조 텍스트(밝은 배경 대비 확보) */
--accent-solid: var(--accent-500) /* 버튼 배경 */
--accent-hover: var(--accent-600)
--accent-subtle: color-mix(in srgb, var(--accent-500) 12%, transparent) /* 선택 하이라이트 */

/* 상태 */
--success-fg: var(--green-500)
--warning-fg: var(--amber-500)
--danger-fg:  var(--red-500)

--shadow-sm: 0 1px 2px rgba(12,14,18,.06)
--shadow-md: 0 4px 12px rgba(12,14,18,.10)
```

### 4.2 다크 테마 (`data-theme="dark"`)

```
/* 배경 */
--bg-canvas:    var(--gray-1000)
--bg-surface:   var(--gray-950)
--bg-subtle:    var(--gray-900)
--bg-inset:     var(--gray-800)

/* 텍스트 */
--fg-default:   var(--gray-100)
--fg-muted:     var(--gray-400)
--fg-subtle:    var(--gray-500)
--fg-on-accent: var(--gray-0)

/* 경계·구분 */
--border-default: var(--gray-800)
--border-strong:  var(--gray-700)

/* 포인트 (어두운 배경에선 더 밝은 단계로) */
--accent-fg:    var(--accent-300)
--accent-solid: var(--accent-500)
--accent-hover: var(--accent-400)
--accent-subtle: color-mix(in srgb, var(--accent-400) 18%, transparent)

/* 상태 */
--success-fg: var(--green-500)
--warning-fg: var(--amber-500)
--danger-fg:  var(--red-500)

--shadow-sm: 0 1px 2px rgba(0,0,0,.4)
--shadow-md: 0 6px 16px rgba(0,0,0,.5)
```

### 4.3 accent 별칭 (블루/퍼플 스위치)

`--accent-*`는 §3의 블루 또는 퍼플 스케일 중 하나를 가리키는 **별칭**이다. 여기 한 곳만 바꾸면
라이트·다크 양쪽의 모든 accent 사용처가 따라온다.

```
:root {
  /* 기본 = 블루. 퍼플로 바꾸려면 --blue-* → --purple-* 로 교체 */
  --accent-300: var(--blue-300);
  --accent-400: var(--blue-400);
  --accent-500: var(--blue-500);
  --accent-600: var(--blue-600);
  --accent-700: var(--blue-700);
}
```

### 4.4 포인트 사용 규칙 (절제)

- accent는 **주요 액션 1개**(예: 저장/검색 버튼), **링크**, **활성 탭·선택 항목**, **포커스 링**에만.
- 넓은 면적(큰 배경 블록)을 accent로 채우지 않는다 — 문서 가독성 우선.
- lint 성공/경고/차단은 accent가 아니라 **상태색(§3.4)** 으로.

## 5. 타이포그래피 · 간격 · 반경

```
--font-sans: system-ui, -apple-system, "Pretendard", "Apple SD Gothic Neo", sans-serif;
--font-mono: "JetBrains Mono", "D2Coding", ui-monospace, SFMono-Regular, monospace;

--text-xs: .78rem;  --text-sm: .875rem; --text-base: 1rem;
--text-lg: 1.125rem; --text-xl: 1.375rem; --text-2xl: 1.75rem;
--leading-body: 1.65;   /* 문서 본문 가독성 */

/* 4px 그리드 */
--space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
--space-5: 24px; --space-6: 32px; --space-8: 48px;

--radius-sm: 4px; --radius-md: 8px; --radius-lg: 12px;
--focus-ring: 0 0 0 3px var(--accent-subtle);
```

- 마크다운 본문은 `--font-sans` + `--leading-body`, **읽기 폭 제한**(약 72ch).
- 코드/앵커/frontmatter는 `--font-mono`.

## 6. 컴포넌트별 적용 (§9 UI 기능 대응)

| 컴포넌트 | 배경 | 텍스트 | 포인트 |
|---|---|---|---|
| 상단 바 / 사이드바 | `--bg-surface` | `--fg-default` | 활성 메뉴 `--accent-fg` |
| 문서 목록 카드 | `--bg-surface` + `--border-default` | 제목 `--fg-default`, 메타 `--fg-muted` | hover 시 `--accent-subtle` |
| 문서 조회(마크다운) | `--bg-canvas` | `--fg-default` | 링크 `--accent-fg` |
| 코드블록 / frontmatter | `--bg-subtle` | mono | — |
| 검색창 · 인풋 | `--bg-subtle` | `--fg-default` | 포커스 `--focus-ring` |
| 검색 결과 출처 앵커 | — | `--fg-muted` mono | 앵커 링크 `--accent-fg` |
| 이력 타임라인(delta) | `--bg-surface` | +줄 `--success-fg`, −줄 `--danger-fg` | — |
| 관계/계보 그래프(mermaid) | `--bg-canvas` | 노드 텍스트 `--fg-default` | 현재 노드 테두리 `--accent-solid` |
| 주요 버튼(저장·검색·생성) | `--accent-solid` | `--fg-on-accent` | hover `--accent-hover` |
| 보조 버튼 | `--bg-subtle` + `--border-strong` | `--fg-default` | — |
| lint 피드백 배너 | 성공/경고/차단 = `--success/warning/danger-fg` + 동색 subtle 배경 | | |
| 마크다운 에디터(CodeMirror) | `--bg-surface` | 토큰 하이라이트는 §7 | 커서/선택 `--accent-*` |
| 테마 토글 | 우상단, 아이콘 버튼 | | |

## 7. 마크다운 에디터 · 코드 하이라이트

- CodeMirror 테마도 **동일 의미 토큰**을 소비한다(별도 색 팔레트 만들지 않음).
- 다크에서 신택스 하이라이트는 무채색 + accent 계열 위주로 저채도 유지(눈부심 방지).
- mermaid는 테마별 config를 주입: 다크에서 `theme: 'dark'` + 배경/선/텍스트를 위 토큰으로 오버라이드.

## 8. 파일 배치 (구현 시)

```
web/static/css/
  tokens.css       # §3 원시 + §4 의미 토큰 (라이트/다크)
  base.css         # 리셋·타이포·본문
  components.css    # §6 컴포넌트 (의미 토큰만 참조)
web/templates/
  _theme-init.html  # <head> 인라인: OS/localStorage 기반 data-theme 결정 (FOUC 방지)
```

- HTMX 부분 스왑 시에도 토큰은 `:root`/`[data-theme]` 전역이라 **조각 HTML에 스타일 재주입 불필요**.

## 9. 비범위 (v0에서 안 함)

- 사용자별 커스텀 테마·색상 피커(관리자 accent 스위치는 §4.3 코드 1곳 교체로 충분).
- 블루·퍼플 **동시 노출** 토글 UI (기본값 택1로 시작 — 필요 시 후속).
- 고대비(High Contrast) 전용 테마, 색맹 시뮬레이션 도구 (접근성 대비 기준만 우선 충족).
- 애니메이션·모션 시스템 (전환은 최소한의 페이드/hover만).

---

**연계:** 이 스펙은 UI 구현(기획안 1 §9.4의 HTMX + 마크다운 에디터)에 적용된다.
사전 요건 — HTTP 엔드포인트(§9.3), 리트리버·이력·lint 결과를 표시하는 읽기/쓰기 뷰.
색상값은 v0이며, 실제 화면 스크린샷 검토로 토큰 **값**만 조정한다(토큰 **구조**는 계약).
