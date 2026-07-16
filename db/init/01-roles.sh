#!/bin/bash
# PostgreSQL 롤 분리 — admin(auth+knowledge) / mcp(knowledge only) (구현스펙-인증인가-RBAC.md §8).
# docker-entrypoint-initdb.d 에서 최초 DB 초기화 시 1회 실행된다(POSTGRES_USER 로).
# 비밀번호는 compose env(HUB_ADMIN_PASSWORD / HUB_MCP_PASSWORD)에서 주입한다.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  -- 롤 생성 (LOGIN 가능한 애플리케이션 계정).
  CREATE ROLE hub_admin LOGIN PASSWORD '${HUB_ADMIN_PASSWORD}';
  CREATE ROLE hub_mcp   LOGIN PASSWORD '${HUB_MCP_PASSWORD}';

  -- knowledge 테이블은 public 스키마 — admin 이 소유·생성·마이그레이션(DDL), mcp 는 CRUD(DML)만.
  -- mcp 에 CREATE 를 주지 않는다: DDL 은 소유 롤(admin)만 수행한다(mcp 는 manage_schema=false 로
  -- DDL 미실행 → 소유권 충돌·기동 순서 크래시 방지, PostgresStore._await_schema 로 준비 대기).
  GRANT ALL ON SCHEMA public TO hub_admin;
  GRANT USAGE ON SCHEMA public TO hub_mcp;
  ALTER DEFAULT PRIVILEGES FOR ROLE hub_admin IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hub_mcp;
  ALTER DEFAULT PRIVILEGES FOR ROLE hub_admin IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO hub_mcp;

  -- auth 테이블(users/web_sessions/personal_access_tokens/auth_audit_log)은 별도 auth 스키마 —
  -- admin 전용. mcp 에는 어떤 권한도 주지 않는다(사용자·PAT·세션을 knowledge 롤로부터 격리).
  CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION hub_admin;
EOSQL
