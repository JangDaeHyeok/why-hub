# 구현 스펙 — M1~M4 워킹 스켈레톤 (Phase별)

> **목적:** 지식 허브의 얇은 수직 슬라이스를 완성한다 — **문서 생성 → save → 검색 → 조회**가
라이브러리·MCP·HTTP 전 경로에서 끝까지 도는 것.
**방법론:** 하네스(스펙 선구동 + 자기 완결 Phase + 순차 실행). 각 Phase는 **그 Phase 파일 + 참조 스펙만으로**
워커 세션이 완수 가능해야 한다(대화 맥락 의존 금지).
> 

---

## 0. 읽는 법 (Claude Code / 워커 세션 지침)

- **자기 완결성:** 각 Phase는 아래 형식을 지킨다 — 목표 / 참조 스펙 / 선행 산출물 / 작업 지침 / 검증.
워커는 참조 스펙 파일을 실제로 열어 읽고 시작한다.
- **참조 스펙(항상 먼저 읽기):** `CLAUDE.md`(불변식·결정), `구현스펙-자체delta엔진-M1.md`(이하 **[Δ]**),
`기획안1`(스코프·도구표), `기획안2`(미래 대비 원칙).
- **불변식 재확인(CLAUDE.md §2):** 모든 쓰기는 save 경유 · 유사 RAG only · 자체 delta · save 게이트 ·
ADR 필수 섹션 · 리트리버 필터-선행.
- **테스트 먼저:** 각 Phase의 "검증"을 테스트로 먼저 작성하고 통과시키는 방향으로 구현.
- **작은 diff:** 요청 범위 밖 리팩터링·의존성 추가 금지.

## 0.1 실행 방식 (하네스 러너 — 개념)

Phase는 파이썬 러너가 `task-index`를 읽어 **헤드리스 Claude Code 세션**으로 순차 실행한다
(`claude --print -p <phase 파일 내용>`). 메인 세션은 의도 파악에만 컨텍스트를 쓰고, 구현은 하위 세션에 위임.
러너 자체의 상세 구현은 이 스펙의 범위 밖(별도). 여기서는 **Phase 파일들과 index**를 정의한다.

## 0.2 "워킹 스켈레톤" 완성 지점

- **P05 끝:** 라이브러리 레벨로 저장+검색 동작(코드로 end-to-end).
- **P08 끝:** 읽기 경로가 MCP·HTTP로 노출(에이전트·API 조회 가능).
- **P09 끝:** **쓰기까지 end-to-end** — 생성 → save → 검색 → 조회 전 경로 완성 = 워킹 스켈레톤 달성.
- P10~P12는 스켈레톤 위의 살(템플릿·계보·인제스천).

---

## 1. task-index

```json
{
  "project": "knowledge-hub",
  "milestones": ["M1", "M2", "M3", "M4"],
  "principles": ["self-contained-phase", "spec-first", "test-first", "small-diff"],
  "phases": [
    {"id": "P01", "m": "M1", "title": "스캐폴딩 & 기반",              "deps": [],            "status": "todo"},
    {"id": "P02", "m": "M1", "title": "정규화·lint·앵커",            "deps": ["P01"],       "status": "todo"},
    {"id": "P03", "m": "M1", "title": "diff·이력·스냅샷",            "deps": ["P01","P02"], "status": "todo"},
    {"id": "P04", "m": "M1", "title": "FTS5 색인·검색",              "deps": ["P01"],       "status": "todo"},
    {"id": "P05", "m": "M1", "title": "락·저널·save·reconcile(통합)", "deps": ["P02","P03","P04"], "status": "todo"},
    {"id": "P06", "m": "M2", "title": "서비스 레이어(인터페이스 독립)", "deps": ["P05"],       "status": "todo"},
    {"id": "P07", "m": "M2", "title": "MCP 서버 · 읽기 4종",         "deps": ["P06"],       "status": "todo"},
    {"id": "P08", "m": "M2", "title": "HTTP API · 읽기",             "deps": ["P06"],       "status": "todo"},
    {"id": "P09", "m": "M3", "title": "쓰기 경로 + docs-diff",       "deps": ["P07","P08"], "status": "todo"},
    {"id": "P10", "m": "M3", "title": "템플릿 + 세션 포크 캡처 워크플로우", "deps": ["P09"],   "status": "todo"},
    {"id": "P11", "m": "M4", "title": "계보(get_related)",           "deps": ["P06"],       "status": "todo"},
    {"id": "P12", "m": "M4", "title": "인제스천 + curate",           "deps": ["P06","P09"], "status": "todo"}
  ]
}
```

> 상태(`todo|doing|done`)는 러너/사람이 갱신. `deps`를 만족한 Phase만 실행한다.
> 

---

## 2. Phase 파일

### P01 — 스캐폴딩 & 기반 (M1)

- **목표:** 패키지 구조·의존성·config·데이터 모델·경로 규칙·테스트 러너를 세운다. 부작용 로직은 없음.
- **참조:** [Δ] §1(모듈 배치), §4(필드/enum), CLAUDE.md §3~4.
- **선행 산출물:** 없음(첫 Phase).
- **작업 지침:**
    - `hub/` 패키지를 [Δ] §1 레이아웃대로 **빈 모듈**로 생성(파일만, 구현은 후속).
    - `hub/models.py`: `Document, Anchor, DiffHunk, HistoryEntry, SaveResult, Hit` 데이터 클래스(순수).
    - `hub/store/paths.py`: `knowledge/{docs/<type>, .snapshots, docs-diff, history, index.sqlite, .locks, .journal}`
    경로 규칙 함수. 경로 조작만, I/O 없음.
    - `config`: dataclass/pydantic — 저장소 루트, 타입별 `id` 정규식, 섹션 별칭(예 "결과"≈"결과 및 영향"),
    락 타임아웃, LLM `base_url`/모델명(옵션). 파일 로드 지원.
    - `pyproject.toml`/`requirements.txt`(허용 도메인 내: pypi), `pytest` 셋업, `hub/tests/`.
- **검증:** 패키지 import 성공 / `paths`가 올바른 경로 반환 / config 파일 로드 테스트 / `pytest` 실행됨.

### P02 — 정규화·lint·앵커 (M1)

- **목표:** save 게이트의 앞부분(정규화·lint)과 앵커 파싱을 완성.
- **참조:** [Δ] §3(정규화), §4(lint), §5.1(앵커), §9(수용 기준 해당 항목).
- **선행 산출물:** P01(models, config, paths).
- **작업 지침:**
    - `normalize.py`: [Δ] §3. **결정론·멱등**, 형식만 손대고 의미 재작성 금지. frontmatter 필드 순서 고정.
    - `anchors.py`: [Δ] §5.1. 헤더 파싱 → `(level, text, slug, path, occurrence, line_range)`, **slug 유일화**(`__2`).
    - `lint.py`: [Δ] §4. 스키마 + 정규화 테스트 + **ADR 필수 섹션(배경/결정/근거/대안/결과)**, 대안·폐기 비면 실패.
        - **id 유일성 검사**는 인덱스가 필요 → 지금은 `exists_fn: Callable[[str], bool]` **프로토콜/콜백으로 주입**
        (P05에서 FTS 조회로 연결). 미주입 시 유일성 검사만 스킵.
    - 실패는 `LintError(reasons: list[str])`, **부작용 이전**에 던진다.
- **검증:** [Δ] §9 "정규화 / lint 게이트 / 앵커·diff(앵커 유일화)" 항목 전부. 게이트 실패 시 파일 미변경.

### P03 — diff·이력·스냅샷 (M1)

- **목표:** 변경 감지(줄 단위 diff, git diff 방식) → 앵커 귀속 → 이력 → 스냅샷.
- **참조:** [Δ] §5.2(diff/귀속), §5.3(이력), §5.4(스냅샷), §9.
- **선행 산출물:** P01, P02(anchors).
- **작업 지침:**
    - `diffing.py`: `difflib` 줄 단위 unified diff → hunk → **감싸는 최근접 헤더(앵커)에 귀속**.
    `old is None`이면 전체 `created`. 여러 앵커에 걸치면 **앵커별로 hunk 분할**.
    - `history.py`: 앵커별 `HistoryEntry`(ts/actor/type/anchor/summary/summary_source/delta). **append-only**.
    `change_type` 자동 판정([Δ] §5.3). **규칙 기반 summary** 기본, LLM은 `summary_source: auto-llm`.
    - `snapshots.py`: load/write/hash(sha256). 해시 불일치 → "전체 created" 안전 처리 + warning.
- **검증:** [Δ] §9 "앵커·diff / 이력·스냅샷" 전부(단일/다중 섹션, 삭제만, created, delta의 +/-, append-only,
deprecation 자동판정, 해시 불일치 처리).

### P04 — FTS5 색인·검색 (M1)

- **목표:** SQLite FTS5 색인·재색인·검색(필터 선행 → MATCH → bm25).
- **참조:** [Δ] §7, CLAUDE.md §2-6(필터 선행), 기획안1 §8.
- **선행 산출물:** P01.
- **작업 지침:**
    - `index_fts.py`: [Δ] §7 스키마(`documents` + `chunks_fts`, **project/tenant 컬럼 NULL로 미리 개설**),
    PRAGMA `WAL`/`synchronous=NORMAL`. `reindex_doc`(앵커별 섹션 upsert, 기존 doc_id 삭제 후 재삽입, `body_hash`).
    - `search(query, filters, k)`: **① `documents`에서 필터로 doc_id 후보 → ② `chunks_fts MATCH` + `doc_id IN 후보`
        - `ORDER BY bm25` LIMIT k**. 결과 `Hit(doc_id, anchor, text, score)` + frontmatter 요약.
    - `id` 존재/유일성 조회 함수 제공(P02 `exists_fn` 연결용).
    - 한국어 토큰화는 `unicode61 remove_diacritics 2`로 시작(커스텀은 후속 과제).
- **검증:** [Δ] §9 "색인·검색" + **필터로 배제된 문서 섹션은 절대 반환 안 됨**(네거티브).

### P05 — 락·저널·save·reconcile (통합) (M1)

- **목표:** save 루틴 완성 + crash-safety. **라이브러리 레벨 워킹 슬라이스 달성.**
- **참조:** [Δ] §2(오케스트레이션 순서), §6(저널/reconcile), §8(파일 락), §9.
- **선행 산출물:** P02(normalize/lint/anchors), P03(diff/history/snapshots), P04(index_fts).
- **작업 지침:**
    - `locking.py`: `doc_lock(id, timeout)` 컨텍스트 매니저, `.locks/<id>.lock`에 `fcntl.flock` 배타.
    타임아웃 → `LockTimeout`. 한 save는 자기 문서 하나만 잠금(데드락 회피).
    - `journal.py`: `.journal/<id>.json`에 op/steps_done/target_paths 기록. begin/commit.
    - `save.py`: [Δ] §2 **단계 순서 그대로**. lint의 `exists_fn`에 P04 유일성 조회 연결. 부작용은 전부 락 안.
    실패 시 저널 근거 롤백/`pending` 처리.
    - `reconcile.py`: [Δ] §6. pending 저널 정리 + 전체 정합성 점검(본문 해시 대조 → 재색인). **멱등**.
- **검증:** [Δ] §9 "락·crash-safety" + **save 라운드트립**(생성→save→검색으로 재확인, 이력 1항목).
동시 save 이력 중복 없음. 중간 종료 후 reconcile 수렴. reconcile 2회 멱등.

### P06 — 서비스 레이어(인터페이스 독립) (M2)

- **목표:** MCP·HTTP가 공통으로 부를 **단일 서비스 계층**. 인터페이스에 로직 중복 금지.
- **참조:** CLAUDE.md §5(모듈 경계), 기획안1 §11(도구/엔드포인트 대응표).
- **선행 산출물:** P05(save/retriever 코어).
- **작업 지침:**
    - `hub/service.py`: `search_knowledge(query, filters)`, `get_document(id)`, `list_documents(...)`,
    `get_history(id, ...)`, `get_docs_diff(id, ...)`, `save_document(raw, actor, ...)` — store를 감싸는 **얇은 파사드**.
    - `get_related`/`ingest_source`/`curate`는 P11/P12에서 추가(시그니처만 미리 예약해도 됨).
    - `actor`는 **인자로** 받는다(인증은 인터페이스가 채움). 서비스는 인증을 모른다.
- **검증:** 서비스 함수 단위 테스트(store 위). 인터페이스 없이도 전 기능 호출 가능.

### P07 — MCP 서버 · 읽기 4종 (M2)

- **목표:** FastMCP로 읽기 도구 노출.
- **참조:** 기획안1 §11(도구표), 기획안2 §1(시그니처 유지 원칙).
- **선행 산출물:** P06.
- **작업 지침:**
    - `hub/interfaces/mcp_server.py`: `search_knowledge / get_document / list_documents / get_history`
    (+ `get_docs_diff`) 도구 → **service 호출만**. 반환에 **출처(id + anchor)** 포함.
    - 쓰기 도구(`save_document` 등)는 P09.
- **검증:** 로컬 MCP 테스트로 검색·조회·목록·이력 동작. 시그니처가 기획안1 §11과 일치.

### P08 — HTTP API · 읽기 (M2)

- **목표:** FastAPI로 읽기 JSON 엔드포인트. **읽기 경로 워킹 스켈레톤.**
- **참조:** 기획안1 §11(HTTP 열). (UI는 M6~M8 — 여기선 JSON API만)
- **선행 산출물:** P06.
- **작업 지침:**
    - `hub/interfaces/http_api.py`: `GET /search`, `GET /docs`, `GET /docs/{id}`, `GET /docs/{id}/history`,
    `GET /docs/{id}/diff` → **service 호출만**.
    - 에러 매핑: `LintError`→422, not found→404, `LockTimeout`→409.
    - CORS/정적 서빙/HTMX는 UI Phase(M6~)에서. 지금은 순수 JSON.
- **검증:** `httpx`로 각 엔드포인트 응답·에러 코드 테스트. 결과에 출처 앵커 포함.

### P09 — 쓰기 경로 + docs-diff (M3)

- **목표:** 인터페이스로 쓰기 노출 + 의도된 변경 기록. **쓰기 end-to-end = 워킹 스켈레톤 달성.**
- **참조:** [Δ] §2(save), 기획안1 §7.3(docs-diff).
- **선행 산출물:** P07, P08.
- **작업 지침:**
    - `save_document` MCP 도구 + `PUT /docs/{id}` HTTP → 둘 다 `service.save_document`(락·delta·이력 자동).
    - `intended_diff` 인자 → `docs-diff/<id>.<date>.md` 기록. `GET /docs/{id}/diff`로 조회.
    - **lint 실패 사유를 인터페이스별로 전달**(HTTP 422 body에 reasons, MCP 에러 메시지).
- **검증:** 쓰기 → 이력 생성 → 검색 반영. 잘못된 문서(대안 섹션 없음) → 422 + 사유. docs-diff 기록/조회.
**생성→save→검색→조회 전 경로 통과 테스트**(스켈레톤 수용).

### P10 — 템플릿 + 세션 포크 캡처 워크플로우 (M3)

- **목표:** Why를 채우는 장치. 코드보다 **템플릿·워크플로우 문서** 중심.
- **참조:** 기획안1 §6(쓰기 경로/Why 캡처), CLAUDE.md §9(도그푸딩).
- **선행 산출물:** P09.
- **작업 지침:**
    - `templates/adr.md`, `templates/design-intent.md`: **lint 필수 섹션과 정확히 정렬**(배경/결정/근거/대안/결과).
    - `docs/workflow-adr-capture.md`: 세션 포크 절차 문서화 — `/branch`(또는 `-fork-session`) → 포크가 초안 →
    사람 검토 → `save_document` → **포크 세션 폐기**(문서만 남고 대화 버림, 메인 컨텍스트 보존).
    - **도그푸딩:** 이 프로젝트의 첫 ADR 몇 개(예: "인덱스=FTS5", "이력=자체 delta")를 이 템플릿으로 실제 작성.
- **검증:** 템플릿으로 만든 문서가 lint 통과. 워크플로우 문서가 자기 완결적(제3자가 그대로 따라 가능).

### P11 — 계보(get_related) (M4)

- **목표:** 결정의 계보(대체·연관) 추적.
- **참조:** 기획안1 §11(get_related), 데이터 모델 `related`/`supersedes`.
- **선행 산출물:** P06(+데이터).
- **작업 지침:**
    - `service.get_related(id)` + MCP 도구 + `GET /docs/{id}/related`.
    - `supersedes` **체인 추적**(A→B→C, 정방향/역방향), `related` **양방향**. **순환 방지**.
- **검증:** 체인·양방향 추적 테스트, 순환 입력에도 종료.

### P12 — 인제스천 + curate (M4)

- **목표:** 온디맨드 인제스천(멱등) + 후보 압축(옵션).
- **참조:** [Δ] §10(ingest는 save 어댑터), 기획안1 §8(curate), CLAUDE.md §3(LLM 클라이언트).
- **선행 산출물:** P09, P06.
- **작업 지침:**
    - `ingest_source(source_ref)`: 소스 읽어 정규화 → **`service.save_document` 호출하는 얇은 어댑터**.
    `source` 키로 기존 문서 찾아 **갱신(멱등)**, 없으면 신규(`type: ingest`). 소스별 실파서(노션/시트)는 기획안 2.
    - `curate(query, candidate_ids)`(옵션): OpenAI 호환 LLM으로 후보 압축. **LLM 미구성 시 graceful skip**.
    - `hub/llm.py`: OpenAI 호환 클라이언트 래퍼(base_url/model config). 호스팅 API로 시작.
- **검증:** 같은 source 재입력 → **신규 아닌 갱신**(멱등). curate on/off 동작. LLM 없을 때 스킵.

---

## 3. 마무리

- P01~P09로 **워킹 스켈레톤**(생성→save→검색→조회, 라이브러리+MCP+HTTP)이 완성된다.
- P10~P12는 Why 캡처 장치·계보·인제스천으로 스켈레톤을 채운다.
- 이후 M5(평가 셋), M6~M8(커스텀 경량 UI: 읽기→쓰기→AI 생성)로 이어진다.

**다음 단계(④):** `/generate` 프롬프트·템플릿 설계(M8) — UI에서 소스 분석 → ADR/MD 초안 생성.
반복 튜닝 대상이라 별도 스펙으로 둔다.