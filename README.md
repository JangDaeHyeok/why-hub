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

# 2) (선택) AI 기능용 LLM 엔드포인트 URL 을 환경변수로 주입 — 미설정 시 AI 기능만 건너뜁니다.
export KNOWLEDGE_HUB_LLM_COMPLETE_URL="https://<논스트리밍-엔드포인트>"
export KNOWLEDGE_HUB_LLM_STREAM_URL="https://<스트리밍-엔드포인트>"

# 3) 웹 관리 서버 실행 → http://127.0.0.1:8000
python -m hub.interfaces.web

# 3) (선택) MCP 서버 실행 — AI 에이전트 접근용
python -m hub.interfaces.mcp_server        # 기본 stdio (인증 off 로컬 전용 — 인증 on 이면 streamable-http 강제)

# 4) (선택) HTTP JSON API 만 띄우기 (웹 UI 없이 순수 JSON) → http://127.0.0.1:8000
python -m hub.interfaces.http_api
```

브라우저에서 `http://127.0.0.1:8000` 을 열면 문서 목록 · 검색 · 작성 · AI 생성 · 승인함을 사용할 수 있습니다.

> **설정**은 작업 디렉토리의 `config.toml`(또는 `KNOWLEDGE_HUB_CONFIG` 환경변수)에서 읽습니다.
> 예시는 [`config.example.toml`](config.example.toml)을 참고하세요.
> AI 기능(검색 요약·초안 생성·멀티턴 채팅)의 **LLM 엔드포인트 URL은 인증 없는 공개 URL(=시크릿성)이라
> git에 커밋하지 않고 환경변수로 주입**합니다 — `KNOWLEDGE_HUB_LLM_COMPLETE_URL`(논스트리밍) ·
> `KNOWLEDGE_HUB_LLM_STREAM_URL`(스트리밍). 템플릿은 [`.env.example`](.env.example)을 복사해 `.env`로 두면
> 됩니다(`.env`는 git 무시). 미설정 시 AI 관련 기능만 자동으로 건너뜁니다. `effort`/`max_tokens`는 비시크릿
> 튜닝값이라 `config.toml`의 `[llm]`에 둡니다.

### 예제 데이터 넣어보기

```bash
python scripts/seed_knowledge.py    # 템플릿·샘플 문서 시드
```

---

## 배포하기 (docker-compose)

배포 시에는 모든 상태를 **PostgreSQL**에 저장합니다(문서·이력·스냅샷·제출·FTS). 트랜잭션으로 안전하게 직렬화됩니다.

```bash
# 시크릿·엔드포인트는 .env 로 주입 (docker compose 가 자동 로드). .env.example 을 복사해 채웁니다.
cp .env.example .env   # KNOWLEDGE_HUB_LLM_*_URL, PGPASSWORD 를 채운다 (.env 는 git 무시)
docker compose up --build
#   → postgres + admin(웹 UI, :8000) + mcp(streamable-http, :8001)

# 최초 1회: 기존 로컬 knowledge/ 를 PostgreSQL 로 이관
docker compose run --rm admin python scripts/import_to_postgres.py
```

- **관리 서버** `http://localhost:8000` — 웹 UI · HTTP API · 승인함 · 인증/JWT 발급
- **MCP 서버** `http://localhost:8001` — 원격 AI 에이전트 접근(streamable-http, JWT 필수)
- 배포 설정은 [`config.deploy.toml`](config.deploy.toml)(`[storage] backend="postgres"`), 로컬은 `config.toml`(file)을 그대로 씁니다.

---

## 인증 · 권한 (Auth · RBAC)

로그인·회원가입·PAT·JWT·역할(member/admin)을 지원합니다. 상세는 [`docs/specs/구현스펙-인증인가-RBAC.md`](docs/specs/구현스펙-인증인가-RBAC.md).

- **웹(사람)** = opaque **세션 쿠키**(HttpOnly·SameSite=Lax·Secure) + CSRF. JWT를 브라우저에 저장하지 않습니다.
- **MCP(에이전트)** = **Bearer JWT**(RS256). admin 서버가 PAT를 단기 JWT로 발급하고, MCP 서버는 공개키로 **검증만** 합니다.
- 역할은 **scope**로 판정: member = `knowledge:read`+`knowledge:submit`, admin = +`knowledge:review`.

> **로컬 개발 기본은 인증 off**(`config.toml`에 `[auth]` 없음 → 무마찰). 인증을 켜려면 `AUTH_ENABLED=true` +
> 아래 키/시크릿을 주입합니다. **배포(docker-compose)는 인증 on이 기본**입니다.

### 로컬에서 로그인/회원가입 켜보기

```bash
# 1) RSA 키 생성 (admin 만 private, MCP 는 public 으로 검증)
mkdir -p secrets
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out secrets/jwt_private.pem
openssl rsa -pubout -in secrets/jwt_private.pem -out secrets/jwt_public.pem

# 2) 시크릿·키 경로를 환경변수로 주입하고 인증을 켠다
export AUTH_ENABLED=true
export AUTH_COOKIE_SECURE=false            # 로컬(HTTP)은 false, 배포(HTTPS)는 true
export AUTH_PAT_PEPPER="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
export AUTH_SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
export AUTH_PRIVATE_KEY_FILE=secrets/jwt_private.pem
export AUTH_PUBLIC_KEY_FILE=secrets/jwt_public.pem

python -m hub.interfaces.web    # http://127.0.0.1:8000/ui/signup 에서 가입 → 로그인
```

`/ui/signup`으로 가입하면 즉시 **active member**가 됩니다. `/ui/account`에서 비밀번호 변경, `/ui/account/tokens`에서 **PAT**를 관리합니다.

### PAT → JWT → MCP 사용

```bash
# 1) 웹 /ui/account/tokens 에서 PAT 발급 (원문은 생성 직후 1회만 표시됨: whp_<id>_<secret>)

# 2) PAT 를 단기 JWT 로 교환 (응답에 Cache-Control: no-store)
curl -s -X POST http://localhost:8000/api/auth/token/exchange \
  -H "Authorization: Bearer whp_<id>_<secret>"
#   → {"access_token":"<JWT>","token_type":"Bearer","expires_in":600,"scope":"knowledge:read knowledge:submit"}

# 3) JWT 를 MCP 요청의 Authorization 헤더로 전달 (streamable-http)
#    예: fastmcp Client(url="http://localhost:8001/mcp/", auth="<JWT>")
curl -s http://localhost:8001/mcp/ -H "Authorization: Bearer <JWT>" -H "Accept: text/event-stream"
```

### 관리자 지정 (DB 로만)

관리자 지정/해제 UI는 없습니다. DB에서 `is_admin`을 바꿉니다(변경 후 재로그인/신규 JWT부터 review 권한 반영).

```sql
-- 로컬(SQLite): sqlite3 knowledge/auth.sqlite
UPDATE users SET is_admin = 1 WHERE username = '관리자사용자명';
-- 배포(PostgreSQL): auth 스키마
UPDATE auth.users SET is_admin = true WHERE username = '관리자사용자명';
```

### 알아둘 특성 · 현재 제한

- **JWT 는 기본 10분(`AUTH_ACCESS_TOKEN_TTL_SECONDS`)** 유효합니다. MCP는 공개키만으로 무상태(stateless) 검증합니다.
- **PAT 를 폐기해도 이미 발급된 JWT 는 즉시 무효화되지 않고 JWT 만료(최대 10분)까지 유효**할 수 있습니다(무상태 검증의 대가).
- **인증이 켜진 상태에서 MCP `stdio` transport 는 기동을 거부**합니다 — stdio는 헤더 기반 Bearer 인증을 실을 수 없어 무인증 노출이 되기 때문입니다. 배포는 `streamable-http`만 사용합니다.
- 비밀번호 변경 시 **현재 세션은 유지, 다른 모든 웹 세션은 로그아웃**됩니다(PAT은 유지).
- 배포는 admin/mcp가 **서로 다른 PostgreSQL 롤**을 씁니다(admin=auth+knowledge, mcp=knowledge only). MCP 컨테이너에는 **private key·PAT pepper를 전달하지 않습니다**(공개키만).

### 프로젝트별 접근 권한 (Project ACL)

전역 역할(member/admin) 위에 **프로젝트별 역할**을 부여합니다.

- **역할**: `viewer`(읽기) / `editor`(읽기+제출). admin 은 모든 프로젝트 전권.
- **기본 프로젝트**(`default_project`)는 모든 로그인 사용자에게 공개(읽기+쓰기). 그 외 프로젝트는 **명시적 부여**가 필요하며, 부여받지 못하면 목록·검색·조회·쓰기 모두 차단됩니다(deny-by-default).
- **관리(admin 전용)**: `/ui/projects` 에서 프로젝트를 생성·수정·**삭제**하고 멤버(접근 권한)를 추가/제거합니다. 관리자 지정처럼 별도 CLI 없이 웹에서 관리합니다. 프로젝트를 삭제하면 접근 권한(멤버십)은 회수되지만 **문서는 남아 이후 관리자만 접근**할 수 있고, **기본 프로젝트는 삭제할 수 없습니다**.
- **강제 위치**: 웹 세션·MCP JWT 모두 인증 주체를 서비스에 전달해 **동일하게** 강제합니다. 권한 필터는 검색·랭킹 **이전**에 적용됩니다(누출 방지).
- **MCP staleness**: 프로젝트 멤버십은 웹에는 즉시 반영되지만, **이미 발급된 JWT 에는 만료(최대 10분) 후 반영**됩니다(무상태 검증). 새 JWT를 교환하면 즉시 최신 권한이 적용됩니다.

프로젝트 멤버십도 DB에서 직접 조회/변경할 수 있습니다(로컬 SQLite `auth.sqlite` / 배포 PostgreSQL `auth` 스키마):

```sql
-- carol 에게 alpha 프로젝트 editor 권한 부여 (예시 — 보통은 /ui/projects 에서 관리)
INSERT INTO project_members(project_slug, user_id, role, created_at)
VALUES ('alpha', '<carol_user_id>', 'editor', '2026-07-15T00:00:00');
```

- 멀티테넌트 분리·프로젝트별 리뷰어 역할은 이번 범위 밖(향후 G1 확장)입니다.

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

**웹 UI** (사람용) — 로그인 · 회원가입 · 내 계정 · PAT 관리 · 목록 · 문서 뷰 · 검색 · 편집 · AI 생성 · 멀티턴 채팅 · 이력 · 승인함(admin)

> 인증이 켜지면 목록·검색·조회·작성·AI·승인함은 **로그인 필수**입니다. 무인증 공개 경로는 로그인·회원가입·정적·`/healthz`·`/.well-known/jwks.json`·`POST /api/auth/token/exchange`(PAT 인증)뿐입니다. actor/approver는 요청/폼이 아니라 **인증 세션**에서만 결정됩니다.

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
