# 구현스펙 — PostgreSQL 저장 백엔드 + 서비스 배포

## 1. 목적·배경

지식 허브를 서버에 배포해 **MCP 서버 + 관리(HTTP/UI) 서버**로 운영하고 데이터를 견고하게 관리한다.
현재는 인덱스만 SQLite이고 문서·이력·스냅샷·제출·락·저널이 로컬 파일이라 다중 컨테이너 배포에서
flock(NFS 취약)·저널 원자성·파일 공유가 문제다. → **모든 상태를 PostgreSQL로** 옮기고 **docker-compose**로
배포, **MCP는 HTTP(streamable-http)** transport.

## 2. 접근 — 스토어 백엔드 추상화 (pluggable)

`KnowledgeService`가 **`Store` 추상**(`hub/store/base.py`)에 의존한다. 두 구현:
- **`FileStore`**(`file_store.py`) — 기존 파일·SQLite 동작을 그대로 감싼 것. **기본값(로컬·테스트)**.
- **`PostgresStore`**(`pg_store.py`) — 모든 상태를 PG 한곳에. 트랜잭션 ACID로 저널/flock 불필요. **배포용**.

config `[storage] backend`로 선택. 로컬/테스트=file(기존 210 테스트 무변경), 배포=postgres.

## 3. Store 인터페이스 (서비스가 쓰는 영속 연산)

- 쓰기: `reflect(raw_markdown, *, actor, change_type, intended_diff, now) -> SaveResult`
  (normalize→lint→diff→history→snapshot→docs_diff→FTS 재색인. project는 서비스가 미리 frontmatter에 주입.)
- 읽기: `get_raw(id)`, `get_meta(id)`, `exists(id)`, `all_doc_ids()`, `list_projects()`,
  `search(tokens, filters, k, *, mode)`, `list_documents(filters, limit, offset)`,
  `read_history(id)`, `read_docs_diff(id, date)`, `all_frontmatter() -> {id: fm}`.
- 제출: `create_submission(...)`, `read_submission(id)`, `list_submissions(status)`, `set_submission_status(...)`.
- 락: `ingest_lock()`, `submissions_lock()` (컨텍스트 매니저). `close()`.
- 문서 dict 조립(normalize+anchors+project coercion)은 **서비스에 유지**(백엔드 중립) — 스토어는 `get_raw`만 제공.

## 4. 질의 dialect 분리

FTS5 문법(`"tok"` AND, `tok OR tok`)이 현재 service에 있음. → 서비스는 `\w+` **토큰 추출만**(중립),
연산자 결합은 각 백엔드 `search(tokens, ..., mode="and"|"or")`가 담당(FileStore=FTS5, PostgresStore=tsquery).

## 5. PostgresStore 스키마 (단일 스키마)

- `documents(id PK, type, status, title, project, tenant, source, author, created, updated,
  tags jsonb, related jsonb, supersedes text, body text, raw text, body_hash text)`
- `snapshots(doc_id PK, body text, body_hash text)`
- `history(doc_id, seq bigserial, ts, actor, type, anchor, summary, summary_source, delta)` — append-only
- `docs_diff(doc_id, date, content, PK(doc_id,date))`
- `chunks(doc_id, anchor, text, tsv tsvector)` + `GIN(tsv)` — 앵커별 섹션 FTS
- `submissions(id PK, op, doc_id, raw_markdown, intended_diff, change_type, project, actor,
  status, prelint jsonb, created, reviewer, reviewed_at, note)`
- tenant 컬럼 예약(기획안2).

## 6. save 파이프라인 (단일 트랜잭션 — 저널/롤백 불필요)

1. `normalize` → 2. `lint`(부작용 전, exists는 store 조회) → 3. `pg_advisory_xact_lock(hashtext(id))`
→ 4. 스냅샷 로드 → 5. `diffing.diff` → 6. `history.determine_change_type`/`build`
→ 7. history insert → 8. documents upsert → 9. snapshot upsert → 10. (옵션)docs_diff upsert
→ 11. chunks 재구성(delete+insert, `to_tsvector('simple', text)`) → COMMIT.
실패 시 트랜잭션 롤백(원자성). project 미지정→`default_project` coercion 동일.

## 7. 검색

필터 WHERE(type/status/project/tenant/source/tags) **선행** → `chunks.tsv @@ to_tsquery('simple', <tokens>)`
(AND=`&`, OR=`|`, 토큰은 정규식 `\w+`이라 안전) → `ts_rank_cd(tsv, query)` DESC LIMIT k. (§2-6 순서 유지.)
한국어는 `simple` config(공백/구두점 토큰화 — 현 FTS5 unicode61과 실질 동등, 임베딩 아님 = §2-2 준수).

## 8. 배포

- `Dockerfile`(python:3.11-slim + requirements[+psycopg[binary]]).
- `docker-compose.yml`: `postgres:16`(볼륨) + `admin`(web `0.0.0.0:8000`) + `mcp`(streamable-http `:8001`).
- 엔트리포인트: web/http main() `HOST`/`PORT` env. mcp main() `KNOWLEDGE_HUB_MCP_TRANSPORT`(stdio|streamable-http)
  + `MCP_HOST`/`MCP_PORT` → `build_mcp(service).run(transport=..., host=..., port=...)`.
- `scripts/import_to_postgres.py`: 기존 `knowledge/`(FileStore) → PostgresStore 일괄 이관.

## 9. 테스트

- `test_store_contract.py`: FileStore 항상 + PostgresStore(`WHYHUB_TEST_DSN` 있을 때만) 파라미터라이즈.
- 기존 210 테스트는 file 기본이라 무변경. PG 전용은 `@pytest.mark.postgres`(DSN 없으면 skip).
- docker로 PG 띄워 통합 스모크 + import 이관 확인.
