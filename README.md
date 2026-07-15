# 지식 허브 (Knowledge Hub)

> **"What이 아니라 Why."**
> 팀의 결정과 그 **이유**를 한곳에 모아, 검색하고 · 이력을 추적하고 · AI로 초안까지 만드는 지식 저장소입니다.

ADR(아키텍처 결정 기록), 설계 의도, 컨벤션 가이드처럼 **"우리는 왜 이렇게 하기로 했나"** 를 담은 문서를
정규화된 마크다운으로 중앙관리합니다. 사람은 **웹 UI**로, AI 에이전트는 **MCP**로, 다른 시스템은 **HTTP API**로
같은 지식에 접근합니다.

---

## 무엇을 할 수 있나요?

- 🔍 **검색** — 키워드로 관련 문서를 찾습니다. (벡터·GPU 없이 가벼운 유사 RAG 방식)
- 📖 **읽기·브라우징** — 프로젝트별 문서 목록, 렌더링된 마크다운, 관련 문서 추천을 웹에서 봅니다.
- ✍️ **쓰기** — 웹 에디터에서 직접 작성하거나, **AI에게 초안 생성**을 맡길 수 있습니다.
- 🤖 **AI 멀티턴 생성** — 대화하며 문서 초안을 다듬고, 원하면 그대로 저장까지(펑션콜 기반).
- 🕐 **이력 추적** — 문서가 언제·어디가·왜 바뀌었는지 **자체 delta 이력**으로 남깁니다. (git 불필요)
- ✅ **승인 워크플로우** — 모든 쓰기를 관리자 승인 대기 큐에 올려 검토 후 반영합니다.
- 🗂 **멀티프로젝트** — 하나의 허브에서 여러 프로젝트의 지식을 프로젝트 단위로 나눠 관리합니다.

### 이 허브가 지키는 원칙

| 원칙 | 의미 |
| --- | --- |
| **모든 쓰기는 save 게이트를 거친다** | 정규화·lint 검사를 통과하지 못한 문서는 저장·색인되지 않습니다. |
| **ADR엔 근거가 필수** | 배경·결정·근거·**대안**·결과 섹션이 없으면 저장을 거부합니다. (Why를 강제) |
| **한 코어, 세 인터페이스** | 웹 UI · MCP · HTTP가 모두 같은 서비스 로직을 호출합니다. |
| **가벼운 스택** | 임베딩·벡터DB·리랭커·GPU를 쓰지 않습니다. |

---

## 빠르게 시작하기 (로컬)

로컬은 별도 DB 없이 **파일 + SQLite(FTS5)** 만으로 동작합니다.

```bash
# 1) 의존성 설치 (Python 3.11+)
pip install -e .

# 2) 웹 관리 서버 실행 → http://127.0.0.1:8000
python -m hub.interfaces.web

# 3) (선택) MCP 서버 실행 — AI 에이전트 접근용
python -m hub.interfaces.mcp_server        # 기본 stdio

# 4) (선택) HTTP JSON API 만 띄우기 (웹 UI 없이 순수 JSON) → http://127.0.0.1:8000
python -m hub.interfaces.http_api
```

브라우저에서 `http://127.0.0.1:8000` 을 열면 문서 목록 · 검색 · 작성 · AI 생성 · 승인함을 사용할 수 있습니다.

> **설정**은 작업 디렉토리의 `config.toml`(또는 `KNOWLEDGE_HUB_CONFIG` 환경변수)에서 읽습니다.
> 예시는 [`config.example.toml`](config.example.toml)을 참고하세요. AI 기능을 쓰려면 `OPENAI_API_KEY`를 설정합니다
> (미설정 시 AI 관련 기능만 자동으로 건너뜁니다).

### 예제 데이터 넣어보기

```bash
python scripts/seed_knowledge.py    # 템플릿·샘플 문서 시드
```

---

## 배포하기 (docker-compose)

배포 시에는 모든 상태를 **PostgreSQL**에 저장합니다(문서·이력·스냅샷·제출·FTS). 트랜잭션으로 안전하게 직렬화됩니다.

```bash
# 비밀·키는 환경변수로 전달 (PGPASSWORD, OPENAI_API_KEY)
docker compose up --build
#   → postgres + admin(웹 UI, :8000) + mcp(streamable-http, :8001)

# 최초 1회: 기존 로컬 knowledge/ 를 PostgreSQL 로 이관
docker compose run --rm admin python scripts/import_to_postgres.py
```

- **관리 서버** `http://localhost:8000` — 웹 UI · HTTP API · 승인함
- **MCP 서버** `http://localhost:8001` — 원격 AI 에이전트 접근(streamable-http)
- 배포 설정은 [`config.deploy.toml`](config.deploy.toml)(`[storage] backend="postgres"`), 로컬은 `config.toml`(file)을 그대로 씁니다.

---

## 인터페이스 한눈에

세 인터페이스 모두 **동일한 서비스 코어**를 호출합니다 — 로직 중복이 없습니다.

**MCP 도구** (AI 에이전트용)
`search_knowledge` · `get_document` · `list_documents` · `list_projects` · `get_history` ·
`get_docs_diff` · `get_related` · `save_document` · `ingest_source` · `curate` ·
`list_submissions` · `approve_submission` · `reject_submission`

**HTTP API** (읽기 JSON + 쓰기/생성)
`GET /search` · `GET /docs` · `GET /docs/{id}` · `PUT /docs/{id}` · `GET /docs/{id}/history` ·
`GET /docs/{id}/diff` · `GET /docs/{id}/related` · `POST /ingest` · `POST /generate` ·
`POST /chat` · `/chat/stream` · `/chat/apply` · `GET/POST /submissions...`

**웹 UI** (사람용) — 목록 · 문서 뷰 · 검색 · 편집 · AI 생성 · 멀티턴 채팅 · 이력 · 승인함

---

## 프로젝트 구조

```
why-hub/
├─ hub/
│  ├─ store/          # 저장 코어: 정규화·lint·앵커·diff·이력·스냅샷·FTS·락·reconcile
│  │  ├─ file_store.py   #   FileStore (로컬: 파일 + SQLite FTS5)
│  │  └─ pg_store.py     #   PostgresStore (배포: 전부 PostgreSQL)
│  ├─ service.py      # 서비스 레이어 — 모든 인터페이스가 부르는 단일 코어
│  ├─ interfaces/     # web(UI) · http_api(JSON) · mcp_server(MCP)
│  ├─ chat.py, llm.py # AI 멀티턴 생성 · OpenAI 호환 LLM 클라이언트
│  ├─ eval/           # 검색 품질 평가 셋(골든 질의)
│  └─ tests/          # 테스트 (Phase별 · 계약 · 워크플로우)
├─ knowledge/         # 지식 저장소 (docs · .snapshots · docs-diff · history · index)
├─ web/               # UI 템플릿(HTMX) · 정적 자원(css/js)
├─ templates/         # ADR · 설계 의도 문서 템플릿
├─ scripts/           # seed · postgres 이관 · 검색 평가
├─ docs/              # 기획안 · 구현 스펙 · 개발 계획 · 프롬프트
├─ config.toml        # 로컬 운영 설정 (이 저장소 도그푸딩용)
├─ docker-compose.yml # 배포: postgres + admin + mcp
└─ CLAUDE.md          # AI 협업 규칙 · 불변식 · 확정 결정
```

### 지식 저장소 레이아웃 (`knowledge/`)

```
docs/{adr,design-intent,guide}/*.md   # 현재 문서 (원천)
.snapshots/*.md                        # diff 기준점
docs-diff/*.md                         # 의도된 변경 (스펙 선구동)
history/*.md                           # 자동 delta 이력 (append-only)
index.sqlite                           # FTS5 검색 인덱스 (FileStore)
```

---

## 테스트

```bash
pip install -e ".[dev]"
pytest                       # 전체
pytest hub/tests/test_p02_normalize_lint_anchors.py   # 개별
```

정규화 멱등성 · 앵커 무결성 · save 라운드트립(생성→save→검색→조회) 등
회귀 방지의 핵심을 검증합니다.

---

## 더 알아보기

- **[CLAUDE.md](CLAUDE.md)** — 프로젝트 불변식, 확정된 구현 결정, 모듈 경계 (AI·기여자 필독)
- **[docs/proposals/](docs/proposals/)** — 왜/무엇: 기획안1(팀 내부 MVP) · 기획안2(범용 확장)
- **[docs/specs/](docs/specs/)** — 어떻게: 자체 delta 엔진 · 앵커 · 멀티턴 생성 · 승인 · 멀티프로젝트 · PostgreSQL 배포 상세 계약
- **[docs/개발-실행계획-산출물인덱스.md](docs/개발-실행계획-산출물인덱스.md)** — 산출물 인덱스 · Phase 실행 순서 · **향후 범용 확장(G1~G5) 로드맵**

---

## 로드맵

현재는 **팀 내부 MVP** 단계입니다(M1~M8 완료: 코어 · 인터페이스 · UI · AI 생성 · 협업 · PostgreSQL 배포).
다음 단계인 **범용 확장(G1~G5)** — 멀티테넌트 · 세밀 권한 · 외부 커넥터 · (조건부) 벡터 검색 — 은
승격 트리거가 실제로 발생할 때 착수합니다. 자세한 계획은 위 개발 실행계획 문서를 참조하세요.
