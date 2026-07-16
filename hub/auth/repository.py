"""AuthRepository — 인증 상태 영속 (구현스펙-인증인가-RBAC.md §7).

지식 Store 와 **책임 분리**한다(문서 저장 불변식에 영향 없음). 두 구현: SQLiteAuthRepository
(로컬/테스트, 별도 auth.sqlite) · PostgresAuthRepository(배포, 같은 DB·별도 테이블). 과도한 ORM
없이 현 sqlite3/psycopg 스타일. 경량 마이그레이션 러너(schema_migrations + 순서 있는 DDL)로
무작정 CREATE TABLE IF NOT EXISTS 확장을 피한다. secret 원문은 저장하지 않는다(해시만).
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path

# ── 컬럼 순서 (SELECT * 매핑용) ──────────────────────────────────────────
USER_COLS = (
    "id", "username", "password_hash", "is_admin", "status",
    "created_at", "updated_at", "password_changed_at",
)
SESSION_COLS = (
    "id", "user_id", "token_hash", "csrf_token",
    "created_at", "expires_at", "revoked_at", "last_seen_at",
)
PAT_COLS = (
    "id", "user_id", "name", "prefix", "secret_hash", "scopes",
    "created_at", "expires_at", "revoked_at", "last_used_at",
)
PROJECT_COLS = ("slug", "name", "description", "created_at", "created_by")

# 프로젝트 역할 (프로젝트별 접근권한, 구현스펙-인증인가-RBAC.md §ACL).
PROJECT_ROLE_VIEWER = "viewer"
PROJECT_ROLE_EDITOR = "editor"
PROJECT_ROLES = (PROJECT_ROLE_VIEWER, PROJECT_ROLE_EDITOR)


class UsernameTaken(ValueError):
    """username unique 위반(회원가입)."""


class ProjectExists(ValueError):
    """project slug unique 위반(프로젝트 생성)."""


def _ddl(bool_type: str, bool_false: str) -> list[str]:
    """dialect 별 v1 스키마 DDL(문장 목록 — 프로토콜 무관하게 개별 실행)."""
    return [
        f"""CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin {bool_type} NOT NULL DEFAULT {bool_false},
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            password_changed_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS web_sessions(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            last_seen_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS web_sessions_user_idx ON web_sessions(user_id)",
        """CREATE TABLE IF NOT EXISTS personal_access_tokens(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            prefix TEXT NOT NULL,
            secret_hash TEXT NOT NULL,
            scopes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_used_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS pat_user_idx ON personal_access_tokens(user_id)",
        """CREATE TABLE IF NOT EXISTS auth_audit_log(
            id TEXT PRIMARY KEY,
            event TEXT NOT NULL,
            user_id TEXT,
            ts TEXT NOT NULL,
            meta TEXT
        )""",
    ]


# 프로젝트 ACL 스키마 v2 (dialect 무관 — bool 없음).
_ACL_DDL = [
    """CREATE TABLE IF NOT EXISTS projects(
        slug TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS project_members(
        project_slug TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        created_at TEXT NOT NULL,
        PRIMARY KEY(project_slug, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS project_members_user_idx ON project_members(user_id)",
]

# 순서 있는 마이그레이션. 새 변경은 새 (version, {...}) 를 append (기존 것 수정 금지).
MIGRATIONS: list[tuple[int, dict[str, list[str]]]] = [
    (1, {"sqlite": _ddl("INTEGER", "0"), "postgres": _ddl("BOOLEAN", "false")}),
    (2, {"sqlite": _ACL_DDL, "postgres": _ACL_DDL}),
]


def _split_scopes(s: str | None) -> list[str]:
    return [x for x in (s or "").split() if x]


def _join_scopes(scopes) -> str:
    return " ".join(scopes or [])


def _new_id() -> str:
    return uuid.uuid4().hex


class AuthRepository:
    """인증 저장소 인터페이스 + 공유 SQL 구현. dialect 별 세부는 하위 클래스가 주입한다.

    하위 클래스는 __init__ 에서 `self.dialect`('sqlite'|'postgres'), `self.ph`(placeholder),
    `self._conn`, `self._lock`, `self._integrity_errors`, `self._commit()` 를 설정하고
    `self.migrate()` 를 호출한다.
    """

    dialect: str
    ph: str

    # ── SQL 실행 헬퍼 (placeholder 는 '?' 로 작성 → dialect 로 치환) ──────
    def _q(self, sql: str) -> str:
        return sql if self.ph == "?" else sql.replace("?", self.ph)

    def _exec(self, sql: str, params: tuple = ()):  # noqa: ANN001
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._q(sql), params)
            self._commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._q(sql), params)
            return cur.fetchall()

    def _commit(self) -> None:  # postgres autocommit → override to no-op
        self._conn.commit()

    # ── 마이그레이션 ──────────────────────────────────────────────────
    def migrate(self) -> None:
        self._exec(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        applied = {r[0] for r in self._query("SELECT version FROM schema_migrations")}
        import datetime

        for version, per_dialect in MIGRATIONS:
            if version in applied:
                continue
            for stmt in per_dialect[self.dialect]:
                self._exec(stmt)
            self._exec(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.datetime.now().isoformat(timespec="seconds")),
            )

    # ── users ─────────────────────────────────────────────────────────
    def create_user(
        self, *, username: str, password_hash: str, is_admin: bool = False,
        status: str = "active", now: str, user_id: str | None = None,
    ) -> dict:
        uid = user_id or _new_id()
        try:
            self._exec(
                "INSERT INTO users(id, username, password_hash, is_admin, status, "
                "created_at, updated_at, password_changed_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, username, password_hash, is_admin, status, now, now, now),
            )
        except self._integrity_errors as e:  # username UNIQUE
            raise UsernameTaken(f"이미 존재하는 사용자명: {username}") from e
        return self.get_user_by_id(uid)  # type: ignore[return-value]

    def get_user_by_username(self, username: str) -> dict | None:
        rows = self._query(
            f"SELECT {', '.join(USER_COLS)} FROM users WHERE username=?", (username,)
        )
        return self._user_row(rows[0]) if rows else None

    def get_user_by_id(self, user_id: str) -> dict | None:
        rows = self._query(
            f"SELECT {', '.join(USER_COLS)} FROM users WHERE id=?", (user_id,)
        )
        return self._user_row(rows[0]) if rows else None

    def update_user_password(self, user_id: str, password_hash: str, now: str) -> None:
        self._exec(
            "UPDATE users SET password_hash=?, password_changed_at=?, updated_at=? WHERE id=?",
            (password_hash, now, now, user_id),
        )

    def set_user_admin(self, user_id: str, is_admin: bool, now: str) -> None:
        self._exec(
            "UPDATE users SET is_admin=?, updated_at=? WHERE id=?", (is_admin, now, user_id)
        )

    def set_user_status(self, user_id: str, status: str, now: str) -> None:
        self._exec(
            "UPDATE users SET status=?, updated_at=? WHERE id=?", (status, now, user_id)
        )

    def _user_row(self, r: tuple) -> dict:
        d = dict(zip(USER_COLS, r))
        d["is_admin"] = bool(d["is_admin"])
        return d

    # ── web_sessions ──────────────────────────────────────────────────
    def create_session(
        self, *, user_id: str, token_hash: str, csrf_token: str,
        expires_at: str, now: str, session_id: str | None = None,
    ) -> dict:
        sid = session_id or _new_id()
        self._exec(
            "INSERT INTO web_sessions(id, user_id, token_hash, csrf_token, created_at, "
            "expires_at, revoked_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, user_id, token_hash, csrf_token, now, expires_at, None, now),
        )
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> dict | None:
        rows = self._query(
            f"SELECT {', '.join(SESSION_COLS)} FROM web_sessions WHERE id=?", (session_id,)
        )
        return dict(zip(SESSION_COLS, rows[0])) if rows else None

    def get_session_by_token_hash(self, token_hash: str) -> dict | None:
        rows = self._query(
            f"SELECT {', '.join(SESSION_COLS)} FROM web_sessions WHERE token_hash=?",
            (token_hash,),
        )
        return dict(zip(SESSION_COLS, rows[0])) if rows else None

    def touch_session(self, session_id: str, last_seen_at: str) -> None:
        self._exec(
            "UPDATE web_sessions SET last_seen_at=? WHERE id=?", (last_seen_at, session_id)
        )

    def revoke_session(self, session_id: str, now: str) -> None:
        self._exec(
            "UPDATE web_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (now, session_id),
        )

    def revoke_sessions_for_user(
        self, user_id: str, now: str, *, except_session_id: str | None = None
    ) -> None:
        """사용자의 세션 폐기(비번 변경 시 다른 세션 폐기 — except 는 유지)."""
        if except_session_id:
            self._exec(
                "UPDATE web_sessions SET revoked_at=? WHERE user_id=? AND id<>? "
                "AND revoked_at IS NULL",
                (now, user_id, except_session_id),
            )
        else:
            self._exec(
                "UPDATE web_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now, user_id),
            )

    # ── personal_access_tokens ────────────────────────────────────────
    def create_pat(
        self, *, token_id: str, user_id: str, name: str, prefix: str, secret_hash: str,
        scopes, expires_at: str | None, now: str,
    ) -> dict:
        self._exec(
            "INSERT INTO personal_access_tokens(id, user_id, name, prefix, secret_hash, "
            "scopes, created_at, expires_at, revoked_at, last_used_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token_id, user_id, name, prefix, secret_hash, _join_scopes(scopes),
             now, expires_at, None, None),
        )
        return self.get_pat(token_id)  # type: ignore[return-value]

    def get_pat(self, token_id: str) -> dict | None:
        """PAT 전체(secret_hash 포함 — 교환 검증용). 없으면 None."""
        rows = self._query(
            f"SELECT {', '.join(PAT_COLS)} FROM personal_access_tokens WHERE id=?", (token_id,)
        )
        return self._pat_row(rows[0]) if rows else None

    def list_pats(self, user_id: str) -> list[dict]:
        """사용자의 PAT 목록 — **secret_hash 제외**(name/prefix/scope/만료/마지막사용만)."""
        rows = self._query(
            f"SELECT {', '.join(PAT_COLS)} FROM personal_access_tokens WHERE user_id=? "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        out = []
        for r in rows:
            d = self._pat_row(r)
            d.pop("secret_hash", None)
            out.append(d)
        return out

    def revoke_pat(self, pat_id: str, user_id: str, now: str) -> bool:
        """**자신의** PAT만 폐기(user_id 일치 조건). 폐기했으면 True."""
        cur = self._exec(
            "UPDATE personal_access_tokens SET revoked_at=? "
            "WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (now, pat_id, user_id),
        )
        return (cur.rowcount or 0) > 0

    def touch_pat(self, pat_id: str, last_used_at: str) -> None:
        self._exec(
            "UPDATE personal_access_tokens SET last_used_at=? WHERE id=?",
            (last_used_at, pat_id),
        )

    def _pat_row(self, r: tuple) -> dict:
        d = dict(zip(PAT_COLS, r))
        d["scopes"] = _split_scopes(d["scopes"])
        return d

    # ── users 목록 (멤버 추가 드롭다운용) ──────────────────────────────
    def list_users(self) -> list[dict]:
        rows = self._query(
            "SELECT id, username, is_admin FROM users ORDER BY username"
        )
        return [{"id": r[0], "username": r[1], "is_admin": bool(r[2])} for r in rows]

    # ── projects (프로젝트별 ACL — admin 관리) ─────────────────────────
    def ensure_project(self, slug: str, name: str, now: str) -> None:
        """프로젝트가 없으면 생성(멱등 — default_project 부트스트랩용)."""
        self._exec(
            "INSERT INTO projects(slug, name, description, created_at, created_by) "
            "VALUES (?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
            (slug, name, None, now, None),
        )

    def create_project(self, *, slug: str, name: str, description: str | None,
                       created_by: str | None, now: str) -> dict:
        try:
            self._exec(
                "INSERT INTO projects(slug, name, description, created_at, created_by) "
                "VALUES (?,?,?,?,?)",
                (slug, name, description, now, created_by),
            )
        except self._integrity_errors as e:
            raise ProjectExists(f"이미 존재하는 프로젝트: {slug}") from e
        return self.get_project(slug)  # type: ignore[return-value]

    def get_project(self, slug: str) -> dict | None:
        rows = self._query(
            f"SELECT {', '.join(PROJECT_COLS)} FROM projects WHERE slug=?", (slug,)
        )
        return dict(zip(PROJECT_COLS, rows[0])) if rows else None

    def update_project(self, slug: str, *, name: str, description: str | None) -> None:
        self._exec(
            "UPDATE projects SET name=?, description=? WHERE slug=?",
            (name, description, slug),
        )

    def delete_project(self, slug: str) -> None:
        """프로젝트 등록 + 멤버십 삭제(접근권한 회수). 지식 문서는 건드리지 않는다."""
        self._exec("DELETE FROM project_members WHERE project_slug=?", (slug,))
        self._exec("DELETE FROM projects WHERE slug=?", (slug,))

    def list_all_projects(self) -> list[dict]:
        """모든 프로젝트 + 멤버 수(admin 관리 화면)."""
        rows = self._query(
            "SELECT p.slug, p.name, p.description, p.created_at, p.created_by, "
            "COUNT(m.user_id) FROM projects p "
            "LEFT JOIN project_members m ON m.project_slug=p.slug "
            "GROUP BY p.slug, p.name, p.description, p.created_at, p.created_by "
            "ORDER BY p.slug"
        )
        out = []
        for r in rows:
            d = dict(zip(PROJECT_COLS, r[:5]))
            d["member_count"] = r[5]
            out.append(d)
        return out

    # ── project_members (접근 권한 grant) ──────────────────────────────
    def add_member(self, project_slug: str, user_id: str, role: str, now: str) -> None:
        """멤버 추가/역할 변경(upsert). role ∈ {viewer, editor}."""
        self._exec(
            "INSERT INTO project_members(project_slug, user_id, role, created_at) "
            "VALUES (?,?,?,?) ON CONFLICT(project_slug, user_id) DO UPDATE SET role=?",
            (project_slug, user_id, role, now, role),
        )

    def remove_member(self, project_slug: str, user_id: str) -> None:
        self._exec(
            "DELETE FROM project_members WHERE project_slug=? AND user_id=?",
            (project_slug, user_id),
        )

    def list_members(self, project_slug: str) -> list[dict]:
        rows = self._query(
            "SELECT m.user_id, u.username, m.role FROM project_members m "
            "JOIN users u ON u.id=m.user_id WHERE m.project_slug=? ORDER BY u.username",
            (project_slug,),
        )
        return [{"user_id": r[0], "username": r[1], "role": r[2]} for r in rows]

    def projects_for_user(self, user_id: str) -> dict[str, str]:
        """사용자의 프로젝트 멤버십 {slug: role} (Principal·JWT 클레임 구성용)."""
        rows = self._query(
            "SELECT project_slug, role FROM project_members WHERE user_id=?", (user_id,)
        )
        return {r[0]: r[1] for r in rows}

    # ── auth_audit_log (secret 원문 절대 금지) ─────────────────────────
    def add_audit(self, *, event: str, user_id: str | None = None, meta: str | None = None,
                  now: str) -> None:
        self._exec(
            "INSERT INTO auth_audit_log(id, event, user_id, ts, meta) VALUES (?,?,?,?,?)",
            (_new_id(), event, user_id, now, meta),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SQLiteAuthRepository(AuthRepository):
    """로컬/테스트 — 지식 store 와 분리된 별도 auth.sqlite."""

    dialect = "sqlite"
    ph = "?"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit(isolation_level=None) + 멀티스레드 허용(FastAPI 워커).
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._integrity_errors = (sqlite3.IntegrityError,)
        self.migrate()

    def _commit(self) -> None:  # isolation_level=None → 이미 autocommit
        pass


class PostgresAuthRepository(AuthRepository):
    """배포 — 지식 테이블과 같은 DB, 별도 auth 테이블(admin DB 롤만 접근)."""

    dialect = "postgres"
    ph = "%s"

    def __init__(self, dsn: str):
        import psycopg  # 지연 import (선택 의존)
        from psycopg import errors as pg_errors

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.RLock()
        self._integrity_errors = (pg_errors.UniqueViolation, psycopg.IntegrityError)
        # auth 테이블은 별도 `auth` 스키마에 격리 — knowledge 롤(hub_mcp)에 접근 권한을 주지
        # 않는다(구현스펙-인증인가-RBAC.md §8). admin 롤만 이 스키마를 소유·접근한다.
        with self._conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS auth")
            cur.execute("SET search_path TO auth, public")
        self.migrate()

    def _commit(self) -> None:  # autocommit=True
        pass
