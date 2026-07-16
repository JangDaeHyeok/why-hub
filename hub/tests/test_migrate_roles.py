"""롤 분리 마이그레이션 스크립트 회귀 테스트 (구현스펙-인증인가-RBAC.md §8, 코드리뷰 3).

기존 pgdata 볼륨 업그레이드용 one-shot 스크립트가 idempotent(있으면 ALTER, 없으면 CREATE)하고,
mcp 에 DDL(CREATE ON SCHEMA)을 부여하지 않음을 라이브 DB 없이 검증한다.
"""

from __future__ import annotations

from scripts import migrate_roles


class _FakeCursor:
    def __init__(self, exists: bool):
        self._exists = exists
        self.executed: list = []

    def execute(self, q, params=None):
        self.executed.append(q)

    def fetchone(self):
        return (1,) if self._exists else None


def test_ensure_role_creates_when_missing():
    from psycopg import sql

    cur = _FakeCursor(exists=False)
    migrate_roles._ensure_role(cur, sql, "hub_mcp", "s3cret")
    text = cur.executed[1].as_string(None)
    assert "CREATE ROLE" in text and "hub_mcp" in text
    assert "s3cret" in text  # 비밀번호 리터럴이 안전하게 조립됨


def test_ensure_role_alters_when_present():
    from psycopg import sql

    cur = _FakeCursor(exists=True)
    migrate_roles._ensure_role(cur, sql, "hub_admin", "pw")
    assert "ALTER ROLE" in cur.executed[1].as_string(None)


def test_grants_do_not_give_mcp_create():
    g = migrate_roles._GRANTS
    assert "GRANT USAGE ON SCHEMA public TO hub_mcp" in g
    assert "CREATE ON SCHEMA public TO hub_mcp" not in g  # DDL 은 admin 만
    assert "AUTHORIZATION hub_admin" in g  # auth 스키마는 admin 소유


def test_superuser_dsn_from_env(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_HUB_SUPERUSER_DSN", raising=False)
    monkeypatch.setenv("PGPASSWORD", "pw")
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGUSER", "hub")
    assert migrate_roles._superuser_dsn().startswith("postgresql://hub:pw@db:")


def test_superuser_dsn_explicit_takes_precedence(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_HUB_SUPERUSER_DSN", "postgresql://x@y/z")
    assert migrate_roles._superuser_dsn() == "postgresql://x@y/z"
