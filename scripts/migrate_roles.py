"""기존 PostgreSQL 볼륨용 롤 분리 마이그레이션 (1회성 · idempotent).

`db/init/01-roles.sh` 는 **빈 PGDATA 최초 초기화 때만** 실행된다(docker-entrypoint-initdb.d 규칙).
기존 pgdata 를 유지한 채 업그레이드하면 hub_admin·hub_mcp 롤이 없는데 앱이 즉시 그 롤로 접속 →
admin·mcp 양쪽 기동이 실패한다. 이 스크립트는 **슈퍼유저로** 롤·권한을 idempotent 하게 보정한다
(이미 있으면 no-op). 구현스펙-인증인가-RBAC.md §8.

사용(docker compose 배포):
    # .env 에 HUB_ADMIN_PASSWORD·HUB_MCP_PASSWORD·PGPASSWORD 설정 후:
    docker compose run --rm \
      -e KNOWLEDGE_HUB_SUPERUSER_DSN=postgresql://hub:$PGPASSWORD@postgres:5432/knowledge_hub \
      admin python scripts/migrate_roles.py
    # 이후 docker compose up (admin=DDL 소유, mcp=DML 전용).

env:
    KNOWLEDGE_HUB_SUPERUSER_DSN  슈퍼유저(예: hub) 접속 DSN. 미지정 시 PG* 로 조립.
    HUB_ADMIN_PASSWORD / HUB_MCP_PASSWORD  롤 비밀번호(신규 생성 시 사용).
"""

from __future__ import annotations

import os
import sys

# 롤 생성 후 실행할 권한 보정(반복 실행해도 안전 · idempotent). CREATE ON SCHEMA public 는 mcp 에
# 주지 않는다 — DDL 은 소유 롤(admin)만 수행한다(§8). mcp 는 DML 권한만 받는다.
_GRANTS = """
GRANT ALL ON SCHEMA public TO hub_admin;
GRANT USAGE ON SCHEMA public TO hub_mcp;

-- 신규 테이블(admin 소유)에 대한 mcp 기본 권한.
ALTER DEFAULT PRIVILEGES FOR ROLE hub_admin IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hub_mcp;
ALTER DEFAULT PRIVILEGES FOR ROLE hub_admin IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO hub_mcp;

-- 이미 존재하는(기존 볼륨의) 테이블·시퀀스에도 mcp DML 권한을 보정.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hub_mcp;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hub_mcp;

-- auth 스키마는 admin 전용(mcp 접근 금지 — 사용자·PAT·세션 격리).
CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION hub_admin;
"""


def _ensure_role(cur, sql_mod, role: str, password: str) -> None:
    """롤이 없으면 CREATE, 있으면 ALTER 로 비밀번호를 현재 env 값에 맞춘다(.env 회전 반영).

    CREATE/ALTER ROLE 은 유틸리티문이라 파라미터 바인딩 불가 → psycopg.sql 로 안전 조립
    (식별자·리터럴 이스케이프)."""
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
    verb = "ALTER" if cur.fetchone() is not None else "CREATE"
    cur.execute(
        sql_mod.SQL("{v} ROLE {r} LOGIN PASSWORD {p}").format(
            v=sql_mod.SQL(verb), r=sql_mod.Identifier(role), p=sql_mod.Literal(password)
        )
    )


def _superuser_dsn() -> str:
    dsn = os.environ.get("KNOWLEDGE_HUB_SUPERUSER_DSN")
    if dsn:
        return dsn
    from urllib.parse import quote

    user = quote(os.environ.get("PGUSER", "hub"), safe="")
    pw = os.environ.get("PGPASSWORD", "")
    host = os.environ.get("PGHOST", "postgres")
    port = os.environ.get("PGPORT", "5432")
    db = quote(os.environ.get("PGDATABASE", "knowledge_hub"), safe="")
    auth = f"{user}:{quote(pw, safe='')}@" if pw else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{db}"


def main() -> int:
    import psycopg
    from psycopg import sql

    admin_pw = os.environ.get("HUB_ADMIN_PASSWORD")
    mcp_pw = os.environ.get("HUB_MCP_PASSWORD")
    if not admin_pw or not mcp_pw:
        print("HUB_ADMIN_PASSWORD·HUB_MCP_PASSWORD 환경변수가 필요합니다.", file=sys.stderr)
        return 2

    dsn = _superuser_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        _ensure_role(cur, sql, "hub_admin", admin_pw)
        _ensure_role(cur, sql, "hub_mcp", mcp_pw)
        cur.execute(_GRANTS)
    print("롤 분리 마이그레이션 완료 (hub_admin·hub_mcp 보정, idempotent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
