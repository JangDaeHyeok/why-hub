# CLAUDE.md — 지식 허브 (Knowledge Hub)

이 파일은 Claude Code가 이 저장소에서 작업할 때 **항상 따르는 규칙과 맥락**이다.
상세 배경·의사결정 근거는 `기획안1`(팀 내부 MVP)·`기획안2`(범용)를 참조한다.
CLAUDE.md는 짧고 정확하게 유지한다(길어지면 규칙 준수도가 떨어진다).

---

## 1. 프로젝트 개요

- 팀 내부 **지식 허브**. ADR·설계 의도·컨벤션을 정규화된 마크다운으로 중앙관리하고,
**단일 서버(MCP + HTTP)** 로 검색·조회·이력·저장을 노출한다.
- 철학: **"What이 아니라 Why"**. **유사 RAG(벡터 없음)**. **자체 delta 이력(git 없음)**.
- 로드맵: MVP(M1~M8, 팀 내부) → 범용(G1~G5). **지금은 MVP.**

## 2. 절대 불변식 (INVARIANTS — 위반 금지)

1. **모든 쓰기는 save 루틴을 경유한다.** 문서 파일을 직접 수정·생성하는 우회 경로를 만들지 않는다
(에이전트·UI·인제스천 전부 동일).
2. **검색은 유사 RAG만.** 임베딩·벡터DB·리랭커·GPU를 도입하지 않는다 (규모 벽 = 기획안 2 사안).
3. **지식 저장소의 이력은 자체 delta(스냅샷 diff).** knowledge store에 git을 쓰지 않는다.
(주의: 아래 §8의 git 규칙은 *이 허브의 소스코드 저장소*에 대한 것 — knowledge store와 구분한다.)
4. **save 게이트를 통과하지 못한 문서는 저장·색인하지 않는다** (정규화/lint 실패 시 차단).
5. **ADR은 필수 섹션(배경/결정/근거/대안/결과)이 있어야 저장된다.** 대안·폐기 선택지가 비면 거부.
6. **리트리버 파이프라인 순서는 필터 → 검색 → 큐레이션으로 고정.** (기획안 2에서 권한 필터가 랭킹
*이전*에 들어갈 자리이므로, MVP에서도 이 순서를 깨지 않는다.)

## 3. 확정된 구현 결정 (DECISIONS)

- **저장 백엔드:** **pluggable 스토어**(`hub/store/base.py`의 `Store` 추상). 두 구현:
  - **FileStore**(기본 · 로컬/테스트): SQLite **FTS5**(BM25, WAL) 인덱스 + 파일 기반 문서·스냅샷·이력·제출.
  - **PostgresStore**(배포): 모든 상태를 PostgreSQL에(문서·스냅샷·이력·docs-diff·제출·FTS `tsvector`).
    트랜잭션 ACID로 저널/flock 불필요, `pg_advisory_xact_lock`으로 문서 직렬화. (구현스펙-postgres-배포.md)
- **검색:** 여전히 **유사 RAG(벡터 없음)** — FileStore=FTS5 BM25, PostgresStore=`tsvector`/`ts_rank_cd`. 필터→검색 순서 고정.
- **앵커 규칙:** **git diff 방식** (§3.1).
- **save 동시성:** FileStore=파일 락(flock), PostgresStore=advisory lock + 트랜잭션.
- **이력:** 자체 스냅샷 + delta (git 없음) — 백엔드 무관.
- **스택:** Python / FastMCP(MCP, 배포 시 streamable-http) / FastAPI(HTTP·UI) / pluggable 스토어(SQLite|PostgreSQL).
  배포는 **docker-compose**(postgres + 관리 서버 + MCP 서버).
- **UI:** 커스텀 경량 — FastAPI + HTMX(읽기·브라우징), 편집/AI 생성 뷰엔 마크다운 에디터 컴포넌트.
- **LLM:** 커스텀 HTTP 엔드포인트 클라이언트 (curate·요약·AI 생성·멀티턴 채팅) — Anthropic Messages
  스타일 · 스트리밍/논스트리밍 2종. **호스팅 API(GPU 없음)**. 네이티브 펑션콜은 없어 멀티턴 도구
  루프는 프롬프트 shim(`hub/llm.py`)으로 흡수. `temperature`/`top_p`/`top_k` 미전송(Sonnet 5 제약).
  **엔드포인트 URL 은 인증 없는 공개 URL(=시크릿성)이라 config 에 커밋하지 않고 env 로 주입**
  (`KNOWLEDGE_HUB_LLM_COMPLETE_URL`/`_STREAM_URL`; `load_default` 가 레이어링). effort/max_tokens 만 config.
- **인증/인가 (구현스펙-인증인가-RBAC.md):** 인증은 인터페이스에서(웹=opaque 세션쿠키, MCP=RS256 JWT),
  인가는 인터페이스-독립 **`Principal`(hub/auth) + 공유 `require_scope` policy**. 역할=scope:
  member(`read`+`submit`), admin(+`review`, `users.is_admin` DB로만 지정). **클라이언트 actor/approver
  신뢰 금지** — service actor 는 항상 `Principal.username`. admin 서버만 private key 보유(PAT→JWT 발급),
  MCP 는 public key 로 검증만. secret(private key·PAT pepper·session secret)은 env/파일 주입(커밋 금지).
  인증 활성 시 MCP stdio 금지(streamable-http 강제). `config.approval.admins`·`Config.is_admin` 은 제거됨.
  auth 저장소(`AuthRepository`, SQLite|Postgres)는 지식 Store 와 분리.
- **프로젝트별 ACL:** 전역 RBAC 위에 프로젝트 역할(viewer/editor). admin 전권·기본 프로젝트 전원 공개,
  그 외는 명시적 grant(deny-by-default). 강제는 service 에서 Principal 기준 — 검색·목록은 store 호출 **전**에
  `project__in`(§2-6 필터-선행) 주입, 단건/이력/계보는 문서 project 검사, 쓰기는 **실제 저장될 project**(frontmatter
  포함)로 editor 검사(우회 차단). MCP 는 JWT `projects` 클레임으로 강제(멤버십 변경은 JWT 만료 후 반영). admin 이
  `/ui/projects` 에서 프로젝트·멤버 관리(`projects`·`project_members` 테이블).

### 3.1 앵커 규칙 (git diff 방식)

- 변경 감지는 **줄 단위 diff**(예: `difflib` unified diff)로 직전 스냅샷과 현재 문서를 비교한다.
- 각 diff hunk를 **그 hunk를 감싸는 가장 가까운 상위 헤더**에 귀속시킨다 (git diff가 hunk 헤더에
감싸는 함수/섹션명을 보여주는 방식과 동일).
- **앵커 = 헤더 슬러그 + 유일성 보장**(헤더 경로 + 동일 헤더 발생 순번). 예: `결정`, `결정__2`,
경로형 `결정/대안`. 이력·검색이 앵커에 의존하므로 **앵커는 안정적·유일**해야 한다.
- history의 `delta`는 해당 hunk의 `+`/ 줄(= git diff 스타일)로 기록한다.

## 4. 디렉토리 — 지식 저장소

```
knowledge/
  docs/{adr,design-intent,guide}/*.md   # 현재 문서 (원천)
  .snapshots/*.md                        # diff 기준점 (내부용)
  docs-diff/*.md                         # 의도된 변경 (스펙 선구동)
  history/*.md                           # 자동 delta 이력 (append-only)
  index.sqlite                           # FTS5 인덱스
```

## 5. 모듈 경계 (섞지 말 것)

- **save 루틴:** 정규화 → lint 게이트 → 스냅샷 diff → 이력 append → 스냅샷 갱신 → FTS 재색인 → actor 기록.
하나의 단계로 감싸 실패 시 롤백/재시도.
- **리트리버:** 구조/메타 필터 → FTS(BM25) → (옵션)curate. **파이프라인 순서 고정**(§2-6).
- **인터페이스:** MCP 도구 / HTTP 엔드포인트는 **같은 코어를 호출**한다. 로직 중복 금지.
- **정규화·lint:** save의 첫 관문 + CI.

## 6. 코딩 규칙

- **작은 diff.** 요청하지 않은 리팩터링·의존성 추가 금지.
- 변경 로직엔 **테스트 추가/수정**. 특히 정규화 테스트는 회귀 방지의 핵심.
- 타입 힌트, 명확한 함수 경계. 설정은 상수 하드코딩보다 **config**.
- 파일 I/O·인덱스 쓰기는 **실패 처리 필수**(롤백/재시도 + reconcile). **원자성 가정 금지**
(파일+인덱스는 진짜 트랜잭션이 아니다 — reconcile로 수렴).

## 7. 테스트 (초기부터)

- **정규화 테스트:** 멱등성 `normalize(normalize(x))==normalize(x)`, 앵커 무결성, 섹션 추출, 포맷 유효성.
- **save 라운드트립:** 생성 → save → 검색 → 조회가 UI 없이 끝까지 도는 **수직 슬라이스**.
- (기획안 2 대비) **권한 네거티브 테스트** — MVP엔 없지만 리트리버의 필터-선행 구조를 깨지 않는다.

## 8. Git — *이 허브의 소스코드 저장소* (knowledge store 아님)

- **요청 전에는 절대 커밋하지 않는다.**
- 커밋 요청 시: **해당 프로젝트의 git config를 사용.** **Co-Authored-By 금지**, "Claude가 커밋/작성했다"는
흔적을 남기지 않는다.
- 커밋은 작은 단위, 명확한 메시지.

## 9. 작업 방식 (도그푸딩 — 하네스 방법론)

- **스펙 선구동:** 기능 작업의 첫 단계는 "문서(스펙/ADR) 갱신 계획" → `docs-diff` 생성 → 구현.
- **ADR 캡처:** 설계 결정이 나오면 **세션 포크**(`/branch`)로 ADR 초안 작성 → 사람 검토 → save.
(이 프로젝트의 실제 ADR이 구현 과정에서 자연스럽게 쌓이게 한다.)

## 10. 하지 말 것 (DON'T)

- 임베딩/벡터/리랭커/GPU 도입 (유사 RAG 유지 — 백엔드가 PostgreSQL이어도 tsvector까지, 임베딩 금지)
- save 루틴(= 스토어 `reflect` 파이프라인)을 우회한 직접 쓰기
- knowledge store에 git 사용
- 스토어 백엔드별 로직을 서비스/인터페이스에 노출 (dialect·영속은 `Store` 구현 안에)
- 리트리버에서 필터를 검색/랭킹 뒤로 미루는 구조 (기획안 2 대비)
- ADR 필수 섹션(특히 대안·폐기 선택지) 없이 저장
- 요청 전 git 커밋 / Co-Authored-By / Claude 흔적