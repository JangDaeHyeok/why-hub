# 구현 스펙 — 자체 delta 엔진 (M1)

> **범위:** 지식 저장소의 저장·정규화·이력·스냅샷·색인 코어. git 없음. 벡터 없음.
**소비자:** save 루틴은 MCP `save_document` / HTTP `PUT /docs/{id}` / `ingest_source`가 공통으로 호출한다.
**불변식:** CLAUDE.md §2 준수 — 모든 쓰기는 이 엔진을 경유하며, 게이트 실패 시 저장·색인하지 않는다.
> 

---

## 0. 이 스펙을 읽는 법 (Claude Code 지침)

- 아래 **모듈 배치**대로 파일을 만든다. 한 파일에 뭉치지 말 것.
- 각 함수는 **계약(입력/출력/예외)** 을 그대로 구현한다. 시그니처를 임의로 바꾸지 않는다.
- **수용 기준(§9)의 테스트를 먼저 작성**하고, 그걸 통과시키는 방향으로 구현한다.
- 원자성을 가정하지 않는다. 파일+SQLite는 진짜 트랜잭션이 아니다 → **저널 + reconcile**로 수렴(§6).

## 1. 모듈 배치

```
hub/
  store/
    paths.py         # 경로 규칙 (docs/.snapshots/history/docs-diff/index.sqlite)
    normalize.py     # 정규화 (§3)
    lint.py          # 스키마·정규화 테스트 게이트 (§4)
    anchors.py       # 헤더 파싱 → 앵커 (§5.1)
    diffing.py       # 줄 단위 diff → hunk → 앵커 귀속 (§5.2)
    history.py       # 이력 항목 생성·append (§5.3)
    snapshots.py     # 스냅샷 읽기/쓰기/해시 (§5.4)
    index_fts.py     # SQLite FTS5 색인·재색인·검색 (§7)
    locking.py       # 문서별 파일 락 (§8)
    journal.py       # save 저널 (crash-safe, §6)
    save.py          # save 루틴 오케스트레이션 (§2) — 위 모듈들을 순서대로 호출
    reconcile.py     # 정합성 점검·복구 (§6)
  models.py          # 데이터 클래스 (Document, HistoryEntry, DiffHunk, SaveResult ...)
```

## 2. save 루틴 — 오케스트레이션 (`save.py`)

```python
def save_document(raw_markdown: str, *, actor: str, change_type: str | None = None,
                  intended_diff: str | None = None) -> SaveResult:
    """
    모든 쓰기의 단일 진입점.
    change_type: created|revision|deprecation|supersede|ingest (None이면 자동 판정)
    intended_diff: docs-diff(의도된 변경) 원문 (있으면 docs-diff/에 기록)
    반환: SaveResult(id, change_type, anchors_changed: list[str], history_id, warnings: list[str])
    예외: LintError(사유 목록) / LockTimeout / StoreCorruption
    """
```

**단계 (반드시 이 순서):**

1. **파싱 + 정규화** — `normalize(raw_markdown)` → `(frontmatter, body_normalized)`. `id` 추출.
2. **lint 게이트** — `lint(doc)`; 실패 시 **여기서 중단**하고 `LintError(reasons)`를 던진다.
(아무 파일도 건드리지 않은 상태여야 함 — 게이트는 부작용 이전)
3. **문서 락 획득** — `with doc_lock(id):` (§8). 이하 전부 락 안에서.
4. **저널 시작** — `journal.begin(id, op="save")` (§6). 이후 각 부작용을 저널에 기록.
5. **스냅샷 로드** — `snapshots.load(id)` → 없으면 `None`(= 신규/최초).
6. **diff + 앵커 귀속** — `diffing.diff(old=snapshot, new=body_normalized)` → `list[DiffHunk]`
(각 hunk에 `anchor` 귀속). 스냅샷이 `None`이면 전체를 **created**로 처리(§5.2 규칙).
7. **change_type 확정** — 인자로 왔으면 존중, 없으면 자동 판정(§5.3).
8. **이력 항목 생성 + append** — `history.build(...)` → `history.append(id, entry)`.
9. **문서 본문 쓰기** — `docs/<type>/<id>.md` 저장 (정규화된 최종본).
10. **스냅샷 갱신** — `snapshots.write(id, body_normalized)` (+ 해시 기록).
11. **docs-diff 기록**(있을 때) — `docs-diff/<id>.<date>.md`.
12. **FTS 재색인** — `index_fts.reindex_doc(id, ...)` (변경 여부와 무관하게 현재 본문 기준 upsert).
13. **저널 커밋** — `journal.commit(id)`.
14. 반환 `SaveResult`.

> 4~13 중 **어느 단계라도 실패**하면: 저널을 근거로 **롤백 시도**(가능한 파일 되돌림) 후, 되돌릴 수
없으면 저널을 `pending`으로 남기고 예외를 올린다. `pending` 저널은 다음 기동/`reconcile`가 정리한다(§6).
> 

## 3. 정규화 (`normalize.py`)

`normalize(raw) -> NormalizedDoc` 는 **결정론적**이어야 한다.

- **frontmatter 파싱:** YAML 블록 분리. 필드 순서를 **정해진 순서로 재직렬화**(멱등성 핵심).
누락 optional 필드는 넣지 않음(빈 값 삽입 금지). `updated`는 저장 시각으로 세팅.
- **본문 정규화:**
    - 개행 `\n` 통일(CRLF→LF), 파일 끝 개행 1개 보장.
    - 헤더 표기 통일(`#` 뒤 공백 1개), 헤더 앞뒤 빈 줄 1개 규칙.
    - 트레일링 공백 제거, 연속 빈 줄 ≤ 1.
    - **의미를 바꾸는 재작성 금지**(문장 재배열·요약 등 절대 안 함). 정규화는 *형식*만 손댄다.
- **멱등성 계약:** `normalize(normalize(x)) == normalize(x)` (바이트 동일). 테스트로 강제(§9).

## 4. lint 게이트 (`lint.py`)

`lint(doc) -> None`(통과) 또는 `raise LintError(reasons: list[str])`.

**(1) 스키마**

- 필수 frontmatter: `id, type, title, status, created` (+ `type=adr`면 아래 §섹션 규칙).
- `type` ∈ {adr, design-intent, guide, spec, note, reference}; `status` ∈ {proposed, accepted, deprecated, superseded}.
- `id` 형식 `^[a-z]+-[0-9]{4}$`(adr 등) — 타입별 규칙은 config. **유일성**은 인덱스 조회로 검증.
- `related`/`supersedes`의 대상 id가 **실제 존재**(dangling reference 차단). 없으면 실패.

**(2) 정규화/구조 테스트**

- **멱등성:** `normalize(raw) == raw_after_first_normalize` (호출측에서 이미 정규화된 본문을 넣으므로,
재정규화 시 불변 확인).
- **앵커 무결성:** 모든 섹션 앵커가 §5.1 규칙으로 **유일**하게 뽑히는가.
- **섹션 추출 가능성:** 각 앵커 → 비어 있지 않은 섹션 텍스트로 매핑되는가.
- **포맷 유효성:** 유효한 markdown/YAML, 깨진 내부 링크 없음.

**(3) ADR 필수 섹션 (INVARIANT §5)**

- `type=adr`면 섹션 **배경 / 결정 / 근거 / 대안 / 결과** 가 모두 존재해야 함.
- **대안·폐기 선택지 섹션이 비어 있으면 실패**(공백/플레이스홀더만 있으면 비어 있음으로 간주).
- 섹션 매칭은 헤더 텍스트 정규화 후 별칭 허용(config: 예 "결과"≈"결과 및 영향").

> lint는 **부작용 이전**에 돈다. 실패 사유는 사람이 읽을 수 있게 반환 → UI가 그대로 표시(9.2 of 기획안 1).
> 

## 5. 앵커 · diff · 이력 · 스냅샷

### 5.1 앵커 (`anchors.py`) — git diff 방식의 "섹션 식별"

- 본문을 헤더 기준으로 파싱해 **(level, text, slug, path, occurrence, line_range)** 목록을 만든다.
- `slug` = 헤더 텍스트 정규화(소문자화는 하지 않음; 한글 유지, 공백→, 특수문자 제거).
- **유일성 보장:** 같은 slug가 여러 번이면 `slug`, `slug__2`, `slug__3` …. 필요 시 경로형 `부모/자식`도 지원.
- `path` = 상위 헤더 슬러그들의 체인(예: `결정/대안`). 앵커 참조는 **slug(순번 포함)** 를 1급으로 쓰고,
path는 보조.
- 계약: `anchors(body) -> list[Anchor]`, 각 `Anchor.line_range`로 §5.2가 hunk를 귀속시킨다.

### 5.2 diff + 귀속 (`diffing.py`) — 줄 단위, git diff 스타일

- `diff(old: str | None, new: str) -> list[DiffHunk]`.
- `old is None` → **전체를 created**로 간주: hunk 하나에 `type=created`, delta는 전체 본문(또는 요약 마커).
- 그 외:
    1. `difflib`(또는 동등)로 **줄 단위 unified diff** 생성.
    2. 각 hunk의 변경 줄 위치를 `new`의 앵커 `line_range`에 대입해 **감싸는 최근접 헤더(앵커)** 를 찾는다
    (git이 hunk 헤더에 함수/섹션명을 붙이는 것과 동일 원리).
    3. `DiffHunk(anchor, added: list[str], removed: list[str])`. 삭제만 있는 hunk는 삭제 위치 직전 헤더에 귀속.
- **여러 앵커에 걸친 변경**은 앵커별로 hunk를 분할한다(이력 항목이 앵커 단위로 나오도록).

### 5.3 이력 (`history.py`)

- `build(id, hunks, actor, change_type) -> list[HistoryEntry]` — **앵커별 1항목**(전역 요약이 필요하면
별도 마커 항목 추가 가능).
- 항목 스키마(YAML, append-only):
    
    ```yaml
    - ts: 2026-07-01T10:20:00  actor: alice  type: revision            # created|revision|deprecation|supersede|ingest  anchor: "결정"  summary: "결정 섹션 변경: JWT → 서버 세션"   # 규칙 기반 (LLM 추정 시 아래 필드)  summary_source: rule       # rule | auto-llm  delta: |    - JWT, 만료 15분    + 서버 세션 + Redis
    ```
    
- **change_type 자동 판정:** 스냅샷 없음→`created`; `status`가 accepted→deprecated 전이→`deprecation`;
`supersedes` 신규 등장→`supersede`; 인제스천 경유→`ingest`; 그 외→`revision`.
- **summary(기본=규칙 기반):** "N개 섹션/문단 변경" 류의 기계 요약. LLM 요약은 옵션이며 반드시
`summary_source: auto-llm` 표기. **`delta`(무엇)는 항상 정확**하므로 요약이 의심되면 delta가 근거.
- append는 **파일 append-only**(기존 항목 수정·삭제 금지).

### 5.4 스냅샷 (`snapshots.py`)

- `load(id) -> str | None`, `write(id, body) -> None`, `hash(id) -> str | None`.
- 스냅샷은 **정규화된 본문**을 저장(= 다음 저장의 diff 기준점).
- **해시 기록:** 스냅샷 옆에 sha256 저장. `load` 시 해시 불일치면 **손상**으로 간주 → 해당 저장을
"전체 created"로 안전 처리(§2-6 규칙과 일치)하고 `warnings`에 남긴다.

## 6. crash-safety — 저널 + reconcile (`journal.py`, `reconcile.py`)

파일+SQLite 이중 쓰기의 **원자성은 불가능**하다. 대신:

- **저널:** `journal.begin/commit`은 진행 중인 save의 의도와 완료 스텝을 작은 파일에 남긴다
(`.journal/<id>.json`: op, id, steps_done[], target_paths[]).
- **크래시 후 기동/주기 실행 시 `reconcile.run()`:**
    1. `pending` 저널을 찾아 마지막 완료 스텝을 확인 → 미완이면 **롤백**(문서/스냅샷/이력/색인을 저널 이전
    상태로 되돌리거나 재적용) 후 저널 제거.
    2. 저널과 무관하게, **전체 정합성 점검**: `docs/`의 각 문서가 FTS에 최신으로 색인돼 있는지
    (본문 해시 대조) → 불일치면 **재색인**. 이력/스냅샷 존재 여부 점검.
- reconcile는 **멱등**해야 한다(여러 번 돌려도 결과 동일).
- 부분 실패한 save는 재시도 큐로 격리하는 것을 권장(최소한 로그 + reconcile 대상).

## 7. SQLite FTS5 색인 (`index_fts.py`)

- **스키마:**
    
    ```sql
    -- 메타(권한/필터/조회용). 기획안 2의 project/tenant는 지금은 NULL 허용으로 미리 열어둠.CREATE TABLE documents(  id TEXT PRIMARY KEY, type TEXT, status TEXT, title TEXT, path TEXT,  tags TEXT, source TEXT, updated TEXT,  project TEXT, tenant TEXT,                 -- MVP: NULL. 기획안 2에서 사용.  body_hash TEXT                             -- reconcile 대조용);-- 섹션(앵커) 단위 FTS. 검색은 섹션 단위로 반환(출처 앵커 포함).CREATE VIRTUAL TABLE chunks_fts USING fts5(  doc_id UNINDEXED, anchor UNINDEXED, text,  tokenize = 'unicode61 remove_diacritics 2');
    ```
    
- **PRAGMA:** `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`.
- `reindex_doc(id, doc)`: 해당 문서의 `documents` upsert + `chunks_fts`에서 기존 doc_id 행 삭제 후
**앵커별 섹션 텍스트 재삽입**. `body_hash` 갱신.
- `search(query, filters, k) -> list[Hit]`:
    1. **필터 선행** — `documents`에서 `type/status/tags`(+장래 `project`) 조건으로 `doc_id` 후보 집합.
    (CLAUDE.md §2-6: 필터 → 검색 순서 고정. 후처리 필터에 의존하지 않음.)
    2. `chunks_fts MATCH :query` + `doc_id IN 후보` + `ORDER BY bm25(chunks_fts)` LIMIT `k`.
    3. `Hit(doc_id, anchor, text, score)` 반환. 상위 결과에 frontmatter 요약 첨부.
- 한국어 토큰화: `unicode61`로 시작하되, 품질 부족 시 별칭/커스텀 토크나이저는 **후속 과제**(스펙 밖).

## 8. 파일 락 (`locking.py`)

- **문서별 배타 락.** 구현: 저장소 안 `.locks/<id>.lock` 파일에 `fcntl.flock`(POSIX) 배타 잠금.
(Windows 지원 필요 시 `portalocker` 등으로 추상화 — 인터페이스는 컨텍스트 매니저로 고정.)
- 계약: `doc_lock(id, timeout=…)` 컨텍스트 매니저. 타임아웃 초과 시 `LockTimeout`.
- **락 범위:** save 루틴의 부작용 단계(§2의 4~13) 전체. lint(2)는 락 밖에서 가능하나, 최종 판정을 위해
**id 유일성 검사는 락 안에서** 재확인(경쟁 방지).
- FTS 쓰기 자체는 SQLite가 직렬화하지만, **문서 단위 경쟁**(동일 문서 동시 save)은 파일 락으로 막는다.
- 데드락 회피: 한 save가 잠그는 문서는 **자기 자신 하나**. 다중 문서 잠금이 필요한 연산은 만들지 않는다.

## 9. 수용 기준 (테스트 먼저 작성)

**정규화**

- [ ]  `normalize(normalize(x)) == normalize(x)` (여러 샘플, 바이트 동일)
- [ ]  frontmatter 필드 순서·개행이 항상 동일하게 재직렬화됨
- [ ]  정규화가 본문의 *의미*를 바꾸지 않음(문장 보존)

**lint 게이트**

- [ ]  필수 필드 누락/enum 위반/`id` 형식 위반 → `LintError`, **파일 미변경**
- [ ]  dangling `related`/`supersedes` → `LintError`
- [ ]  ADR에서 **대안 섹션 비어 있음 → `LintError`**
- [ ]  게이트 실패 시 저장소에 아무 부작용도 없음

**앵커 · diff**

- [ ]  중복 헤더가 `slug`, `slug__2`로 유일화됨
- [ ]  한 섹션만 고친 변경 → 그 **앵커 1개**에만 이력 항목 생성
- [ ]  두 섹션에 걸친 변경 → 앵커별로 **분할된** 이력 항목
- [ ]  삭제만 있는 변경 → 삭제 위치 직전 헤더 앵커에 귀속
- [ ]  신규 문서(스냅샷 없음) → `created` 항목 1개

**이력 · 스냅샷**

- [ ]  `delta`가 `+`/ 줄로 정확히 기록(git diff 스타일)
- [ ]  history는 append-only(기존 항목 불변)
- [ ]  change_type 자동 판정: accepted→deprecated 전이 시 `deprecation`
- [ ]  스냅샷 해시 불일치 → "전체 created" 안전 처리 + `warnings`

**색인 · 검색**

- [ ]  save 후 `chunks_fts`가 앵커별 섹션으로 갱신됨(중복 없음)
- [ ]  `search`가 **필터 선행 → FTS → bm25 정렬** 순으로 동작, 결과에 `anchor` 포함
- [ ]  필터로 배제된 문서의 섹션은 절대 반환되지 않음

**락 · crash-safety**

- [ ]  동일 문서 동시 save → 하나는 대기(또는 `LockTimeout`), 이력 중복 생성 없음
- [ ]  save 중간 강제 종료 후 `reconcile.run()` → 저장소·색인이 일관 상태로 수렴
- [ ]  `reconcile.run()` 멱등(연속 2회 결과 동일)

## 10. 비범위 (이 스펙에서 안 함)

- 임베딩/벡터/리랭킹 (유사 RAG only)
- 권한 강제(토큰·역할·쿼리 필터) — 스키마에 `project/tenant`만 미리 열어둠(기획안 2)
- 커넥터(노션/시트) 인제스천의 소스별 파서 — `ingest_source`는 save를 호출하는 얇은 어댑터로 취급
- UI·`/generate` — 별도 스펙(③, ④)

---

**다음 단계(③):** 이 엔진을 사용하는 **M1~M4 워킹 스켈레톤의 Phase별 구현 스펙**(자기 완결적 Phase 파일들).
`/generate`(④)는 M8에서, 반복 튜닝 대상으로 별도.