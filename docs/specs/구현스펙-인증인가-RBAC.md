# 구현스펙 — 인증/인가(Auth · RBAC)

> 상태: 구현. 범위: MVP(member/admin 2역할). 관련: 기획안2 §4 권한모델(장기), 구현스펙-승인워크플로우.md, 구현스펙-postgres-배포.md.

## 1. 목적·배경

현재 허브는 **인증이 없다.** `actor`/`approver` 는 HTTP 바디·UI 폼·MCP 툴 인자로 클라이언트가
자유 문자열로 보내고, 인가는 `Config.is_admin(actor) = actor in approval.admins` 뿐이다. 즉 누구나
임의 actor 로 위장해 쓰기·승인할 수 있고, MCP streamable-http 는 무인증으로 노출된다.

본 스펙은 **로그인/회원가입 · opaque 웹 세션 · PAT · 단기 RS256 JWT · member/admin RBAC** 를 추가해
이 위장 가능성을 없앤다. 지식 저장 불변식(save/reflect 단일 경로·자체 delta·필터→검색 순서·
FileStore/Postgres 이중 백엔드)은 건드리지 않는다.

## 2. 결정·핵심 원칙

1. **인증은 인터페이스에서, 인가는 공유 policy 로.** 웹=opaque 세션쿠키, MCP=Bearer JWT.
   각 인터페이스가 인증 결과를 **인터페이스-독립 `Principal(user_id, username, is_admin, scopes)`** 로
   변환하고, HTTP·UI·MCP·service 가 동일한 `require_scope()` policy 를 쓴다.
2. **클라이언트가 보낸 actor/approver 는 신뢰하지 않는다.** 요청 모델·폼·MCP 툴에서 actor/approver 를
   제거하고, 서비스에 넘기는 actor 는 항상 `Principal.username`(인증된 세션/JWT)에서 만든다.
3. **역할·scope**: member = `knowledge:read` + `knowledge:submit`, admin = + `knowledge:review`.
   최종 판정은 **scope 기준**. `users.is_admin=true` 이면 JWT/세션 Principal 에 review scope 추가.
4. **관리자 지정은 DB 로만**: `UPDATE users SET is_admin=true WHERE username=…`. 승인/해제 UI 없음.
   기존 `config.approval.admins`·`Config.is_admin` 은 제거한다.
5. **JWT 발급 분리**: admin 웹 서버만 private key 를 갖고 PAT→JWT 를 발급. MCP 서버는 public
   key/JWKS 로 **stateless 검증만** 한다(private key·PAT pepper 미보유). 알고리즘은 서버 설정으로 RS256 고정.
6. **인증 활성 + stdio 금지**: 인증이 켜진 상태에서 MCP transport 가 stdio 면 기동 실패. 배포는
   streamable-http 강제. 로컬 테스트만 명시적으로 인증 비활성 허용.

### 2.1 인가 매핑(도구/엔드포인트 → scope)

| scope | 대상 |
|---|---|
| `knowledge:read` | search_knowledge, get_document, list_documents, list_projects, get_history, get_docs_diff, get_related, curate·읽기기반 AI |
| `knowledge:submit` | save_document, ingest_source, chat apply, 자신의 제출 목록/상세 |
| `knowledge:review` (admin) | 전체 pending 목록, 타인 제출 상세, approve_submission, reject_submission, 웹 승인함 |

서비스 강제 지점: **review 경로**(`approve/reject_submission`)는 `require_scope(SCOPE_REVIEW)` 를 서비스에서
직접 강제(제거한 `is_admin` 대체). **submit/read** 는 인터페이스 경계에서 scope 강제(모든 member 가
전체 열람·제출 → 서비스에 per-doc 필터 불필요, 필터→검색 순서 유지). 제출 목록/상세는 인터페이스에서
member=본인 것만·admin=전체로 필터.

## 3. 웹 세션(opaque) · CSRF

- 로그인 성공 → 충분히 긴 랜덤 세션 토큰 생성. **쿠키엔 원문, DB엔 HMAC/hash 만** 저장.
- 쿠키: `HttpOnly`, `SameSite=Lax`, `Secure`(배포 true·로컬 config 로 false 허용), `Path=/`, 만료.
- 로그아웃 = 서버 세션 폐기 + 쿠키 삭제. 세션마다 CSRF 토큰 발급, 모든 **쿠키 인증 상태변경 요청**에
  CSRF 검증(폼 히든필드/`X-CSRF-Token`). **Bearer 인증 요청엔 CSRF 미요구.**
- **비밀번호 변경**: 현재 세션은 유지, **다른 모든 웹 세션 폐기**. PAT 는 유지(독립 자격증명).
- actor = 세션의 user → `Principal.username`. 요청 body/form 의 actor 는 무시/스키마 거부.

공개(무인증) 경로: 로그인·회원가입·정적 파일·`/healthz`·`/.well-known/jwks.json`·PAT 교환 엔드포인트
(PAT 인증 자체는 필요). 그 외는 로그인 필수. API 오류=JSON 401/403, UI=로그인 redirect.

## 4. 비밀번호

- **Argon2id**(argon2-cffi). 평문 저장 금지. 회원가입 시 최소 정책(길이 등). username 정규화 후 unique.
- 로그인 실패는 사용자 존재여부가 새지 않도록 **동일 메시지**. 로그인·PAT 교환에 기본 rate limit.
- 비번·세션·PAT·JWT 를 로그에 남기지 않는다.

## 5. PAT(Personal Access Token)

- 형식 `whp_<token_id>_<random_secret>`. `random_secret` 충분 엔트로피. DB엔 원문 대신
  **HMAC-SHA256(pepper, secret)** 만. 원문은 생성 직후 **1회만** UI 표시.
- 속성: user·name·prefix·secret_hash·scopes·created_at·expires_at·last_used_at·revoked_at.
- 목록엔 name·prefix·scope·만료·마지막사용만. **자신의 PAT만** 조회·폐기. 생성 시 보유 scope 초과 지정 불가.
- 폐기·만료 PAT 는 JWT 교환 불가. **PAT 폐기는 이미 발급된 JWT 를 즉시 무효화하지 않으며 JWT
  만료(기본 10분)까지 유효**할 수 있음(stateless 검증 대가 — 문서화 필수).

## 6. JWT(RS256)

- `POST /api/auth/token/exchange` — `Authorization: Bearer <PAT>` → `{access_token, token_type,
  expires_in, scope}`, 응답 `Cache-Control: no-store`, rate-limit.
- claim: `iss`, `sub`(user UUID), `aud`(MCP resource id), `iat`, `nbf`, `exp`, `jti`, `scope`,
  표시용 `username`/`is_admin`. **최종 권한은 scope 기준.** header `kid` 포함(키 로테이션 대비).
- 기본 TTL 10분(config). private key 는 admin 서버만. `GET /.well-known/jwks.json` 로 public key 공개.
- 검증: iss·aud 필수, algorithm RS256 고정(토큰 header 값 신뢰 금지). 무토큰·만료·위조 서명·잘못된
  iss/aud·비허용 alg → 401.

## 7. 저장소(AuthRepository) · 마이그레이션

지식 Store 와 **책임 분리**한 `AuthRepository` protocol: `SQLiteAuthRepository`(로컬/테스트, 별도
`auth.sqlite`), `PostgresAuthRepository`(배포, 같은 DB·별도 테이블). 과도한 ORM 없이 현 sqlite3/psycopg
스타일. **경량 마이그레이션 러너**: `schema_migrations(version)` + 순서 있는 `(version, sqlite_sql,
pg_sql)` 목록을 적용여부 체크 후 idempotent 실행(무작정 `CREATE TABLE IF NOT EXISTS` 확장 금지).

테이블: `users`(id·username unique·password_hash·is_admin·status·created_at·updated_at·
password_changed_at), `web_sessions`(id·user_id·token_hash·csrf_token·created_at·expires_at·
revoked_at·last_seen_at), `personal_access_tokens`(id·user_id·name·prefix·secret_hash·scopes·
created_at·expires_at·revoked_at·last_used_at), `auth_audit_log`(event·user_id·ts·최소 메타 —
secret 원문 금지).

## 8. 배포(롤 분리)

- Postgres 롤 2개: `hub_admin`(auth+knowledge 테이블) / `hub_mcp`(knowledge 테이블만). init SQL 로
  롤·GRANT 생성. admin 컨테이너=hub_admin + private key + PAT pepper. mcp 컨테이너=hub_mcp +
  **public key/JWKS 만**(private key·pepper 미전달) + streamable-http + auth on.
- private key·session secret·PAT pepper 는 config.toml 커밋 금지 — env/secret 파일 주입.

## 9. 설정(AuthConfig)

`AUTH_ENABLED`, `AUTH_ISSUER`, `AUTH_MCP_AUDIENCE`, `AUTH_ACCESS_TOKEN_TTL_SECONDS`,
`AUTH_SESSION_TTL_SECONDS`, `AUTH_PRIVATE_KEY_FILE`, `AUTH_PUBLIC_KEY_FILE`|`AUTH_JWKS_URL`,
`AUTH_PAT_PEPPER`, `AUTH_COOKIE_SECURE`, `AUTH_SIGNUP_ENABLED`. secret 은 example 에 이름만.
배포 기본: 인증 on·공개가입 on·secure cookie on·MCP streamable-http. 로컬은 인증 off/테스트키 허용.

## 10. 테스트

회원가입/로그인(중복·해시·동일오류·보호페이지·로그아웃·disabled·쿠키속성·CSRF), PAT(1회표시·원문
미저장·본인만·만료/폐기 교환실패·scope상승차단·정상교환), JWT/MCP(무토큰·정상·만료·위조서명·잘못된
iss/aud·비허용alg·member는 admin툴 거부·admin approve성공·stdio기동거부), actor spoofing(body actor
무시·MCP 인자부재·history/submission actor=인증사용자), 권한(member read/submit·member 승인불가·admin
승인·member 타인제출 불가·admin 전체 pending), 회귀(save gate·lint·승인흐름·FileStore·Postgres·필터→검색).

## 11. 프로젝트별 접근 권한(ACL)

전역 RBAC(member/admin) 위에 **프로젝트별 역할**을 얹는다:

- **역할**: `viewer`(읽기) / `editor`(읽기+제출). 유효 권한 = 전역 scope(read/submit) ∩ 프로젝트 역할.
  admin 은 모든 프로젝트 전권(ACL 우회). **기본 프로젝트(default_project)는 모든 로그인 member 에게 공개**
  (읽기+쓰기 — 하위호환). 그 외 프로젝트는 명시적 grant 필요(없으면 접근 불가, deny-by-default).
- **저장 모델**: auth 저장소의 `projects(slug,name,description,…)` + `project_members(project_slug,user_id,role)`.
  slug = `documents.project` 식별자와 동일. admin 만 프로젝트 생성·수정·삭제·멤버 관리(UI `/ui/projects*`).
  **삭제**는 프로젝트 등록 + 멤버십을 제거해 접근권을 회수한다(지식 문서는 존속 — 이후 admin 만 접근).
  **기본 프로젝트는 삭제 불가**(모든 사용자 공개·미지정 문서 폴백 대상).
- **강제(필터-선행 §2-6)**: `Principal.projects`(=(slug,role)) + 공유 헬퍼(`can_read`/`can_write`/`readable_projects`).
  검색·목록은 store 호출 **이전**에 `project__in`(접근 가능 프로젝트 집합)을 주입하고, 단건 조회·이력·계보는
  대상 문서의 project 로 검사(접근 불가면 미존재와 동일 404). 쓰기는 **실제 저장될 project**(frontmatter 포함)로
  editor 검사 — frontmatter 에 project 를 심어 스코프를 우회하는 것을 차단. 리뷰(승인/반려)는 review scope +
  해당 제출 project 접근.
- **크로스프로젝트 이동(원본+목적지 양쪽 권한)**: 전역 문서 id 는 프로젝트 간 유일하지 않다. 따라서 기존
  문서와 같은 id 를 **다른 목적지 project 로** 저장하면 원본 문서를 덮어쓰며 이동시키게 된다. 목적지 project
  권한만 검사하면 원본 프로젝트에 권한 없는 editor 가 남의 문서를 자기 프로젝트로 탈취할 수 있으므로,
  **이동 쓰기(현재 문서 project ≠ 목적지 project)는 원본·목적지 양쪽 모두 editor 권한을 요구**한다
  (`_assert_can_move`). 저장(즉시 반영)·제출(submit)·승인(approve) 세 지점에서 모두 강제해 우회를 막는다.
- **인터페이스 일관 적용**: HTTP·UI·MCP 모두 인증된 Principal 을 service 에 전달해 동일 강제. 셀렉터·목록은
  접근 가능한 프로젝트만 노출(메뉴 숨김과 별개로 서버에서 강제).
- **MCP(무상태)**: 멤버십을 **JWT `projects` 클레임**으로 실어 보내고 MCP 는 그 클레임으로 강제(auth DB 미접근).
  **멤버십 변경은 웹 세션엔 즉시 반영되지만, 발급된 JWT 에는 만료(≤TTL, 기본 10분) 후 반영**된다.

## 12. 남은 제한(범위 밖)

멀티테넌트(tenant) 분리·프로젝트별 리뷰어 역할·이메일 인증·비번찾기·관리자 사용자관리 UI·PAT scope 세분화·
OIDC/SSO 는 후속(기획안2 §4).

## 13. 보안 하드닝 (코드리뷰 반영)

2차 보안 코드리뷰에서 확인된 결함을 다음과 같이 강화한다(모두 회귀 테스트 동반).

- **DDL 소유 롤 분리(§8 보강)**: 스키마 생성·마이그레이션(DDL)은 소유 롤(hub_admin)만 수행한다.
  검증 전용 롤(hub_mcp)은 `PostgresConfig.manage_schema=false` 로 DDL 을 실행하지 않고 스키마 준비만
  대기·확인한다(마지막 산출물 `submissions.base_hash` 컬럼 폴링). hub_mcp 의 `CREATE ON SCHEMA public`
  부여 제거 → 기동 순서에 따라 mcp 가 테이블 소유자가 돼 admin 의 ALTER 가 not-owner 로 실패하던 크래시 제거.
- **기존 볼륨 마이그레이션**: `db/init` 은 빈 PGDATA 에서만 실행되므로, 기존 볼륨 업그레이드용 idempotent
  one-shot 스크립트 `scripts/migrate_roles.py`(롤·권한 보정) 추가.
- **크로스프로젝트 이동 권한**: §11 참조(원본+목적지 양쪽 editor 요구).
- **로그인 rate limit**: 계정 버킷 + 클라이언트(IP) 버킷 동시 적용(사용자명 회전 우회 차단). 임계값 config.
- **공개 회원가입 rate limit**: 클라이언트(IP) 버킷을 Argon2 해시 이전에 적용(사용자명 회전 자원 소모 차단).
- **로그인·가입 login-CSRF**: 세션-전 상태변경 POST 에 Origin/Referer 동일 출처 검증(교차 출처 차단).
- **계정 열거 방지(타이밍)**: 비활성 계정도 status 검사 이전에 항상 Argon2 검증을 수행해 응답 시간 평준화.
- **채팅 세션 소유자 고정**: 세션 생성자(immutable `owner_user_id`)만 접근·적용 가능(session_id 탈취 차단).
