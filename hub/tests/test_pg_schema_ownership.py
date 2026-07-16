"""PostgresStore 스키마 소유 분리 회귀 테스트 (구현스펙-인증인가-RBAC.md §8, 코드리뷰 1).

DDL(CREATE/ALTER)은 소유 롤(hub_admin, manage_schema=True)만 실행하고, 검증 전용 롤
(hub_mcp, manage_schema=False)은 DDL 을 돌리지 않고 스키마 준비만 대기·확인한다.
라이브 postgres 없이 커넥션을 주입해 실행된 SQL 을 관찰한다.
"""

from __future__ import annotations

import pytest

from hub.config import Config
from hub.store.pg_store import PostgresStore


class _FakeCursor:
    def __init__(self, log: list[str], ready: bool):
        self.log = log
        self._ready = ready
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.log.append(sql)

    def fetchone(self):
        # 스키마 준비 폴링(information_schema)일 때만 결과 반환.
        if "information_schema.columns" in self._last:
            return (1,) if self._ready else None
        return None


class _FakeConn:
    def __init__(self, ready=True):
        self.log: list[str] = []
        self._ready = ready

    def cursor(self):
        return _FakeCursor(self.log, self._ready)

    def close(self):
        pass


def _cfg(manage_schema: bool) -> Config:
    cfg = Config()
    cfg.storage = "postgres"
    cfg.postgres.manage_schema = manage_schema
    return cfg


def test_owner_role_runs_ddl():
    conn = _FakeConn(ready=True)
    PostgresStore(_cfg(True), conn=conn)
    assert any("CREATE TABLE" in s for s in conn.log)  # 소유 롤은 스키마 생성(DDL) 수행
    assert any("ALTER TABLE submissions" in s for s in conn.log)


def test_verify_only_role_skips_ddl():
    conn = _FakeConn(ready=True)  # 스키마 이미 준비됨
    PostgresStore(_cfg(False), conn=conn)
    # mcp 롤은 어떤 DDL 도 실행하지 않는다(소유권 충돌·기동 순서 크래시 방지).
    assert not any("CREATE TABLE" in s for s in conn.log)
    assert not any("ALTER TABLE" in s for s in conn.log)
    # 대신 준비 여부만 확인한다(마지막 마이그레이션 산출물 = submissions.base_hash).
    assert any("information_schema.columns" in s for s in conn.log)


def test_await_schema_times_out_when_not_ready():
    conn = _FakeConn(ready=False)  # base_hash 컬럼 미존재
    with conn.cursor() as cur:
        with pytest.raises(RuntimeError):
            PostgresStore._await_schema(cur, attempts=2, delay=0)


def test_manage_schema_config_layering(tmp_path):
    # TOML: storage.postgres.manage_schema=false 가 반영된다(상수 하드코딩 대신 config).
    p = tmp_path / "c.toml"
    p.write_text(
        "[storage]\nbackend = \"postgres\"\n[storage.postgres]\nmanage_schema = false\n",
        encoding="utf-8",
    )
    cfg = Config.load(p)
    assert cfg.postgres.manage_schema is False
    # 기본값은 True(소유 롤).
    assert Config().postgres.manage_schema is True
