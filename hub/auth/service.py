"""AuthService — 회원가입·로그인·세션·PAT·JWT 교환 오케스트레이션 (구현스펙-인증인가-RBAC.md).

인터페이스(HTTP/UI)는 이 서비스로 인증하고 결과를 `Principal` 로 받는다. 지식 `KnowledgeService`
와 분리된 별도 서비스다(책임 경계). secret(pepper·session secret·private key)은 생성 시 주입한다.
"""

from __future__ import annotations

import datetime
import os
import uuid
from pathlib import Path

import re

from ..config import AuthConfig, Config
from . import passwords, tokens
from .jwt_service import JwtIssuer
from .principal import Principal, scopes_for
from .ratelimit import RateLimiter
from .repository import (
    PROJECT_ROLES,
    AuthRepository,
    PostgresAuthRepository,
    SQLiteAuthRepository,
    UsernameTaken,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def normalize_project_slug(raw: str) -> str:
    """프로젝트 slug 정규화·검증(= documents.project 식별자). 위반 시 ValueError."""
    slug = (raw or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("프로젝트 slug 는 영소문자/숫자로 시작하는 1~64자(영소문자/숫자/._-)여야 합니다.")
    return slug

# 로그인 실패는 사용자 존재여부가 새지 않도록 항상 동일 메시지(§4).
LOGIN_FAILED_MSG = "사용자명 또는 비밀번호가 올바르지 않습니다."


class AuthError(Exception):
    """로그인·PAT 교환 실패(외부 노출 메시지는 존재여부를 노출하지 않게 일반화)."""


class RateLimited(AuthError):
    """rate limit 초과(로그인·PAT 교환)."""


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None


class AuthService:
    def __init__(
        self,
        repo: AuthRepository,
        config: AuthConfig,
        *,
        pat_pepper: str,
        session_secret: str,
        issuer: JwtIssuer | None = None,
        login_limiter: RateLimiter | None = None,
        login_client_limiter: RateLimiter | None = None,
        signup_limiter: RateLimiter | None = None,
        exchange_limiter: RateLimiter | None = None,
    ):
        self.repo = repo
        self.config = config
        self._pepper = pat_pepper
        self._session_secret = session_secret
        self.issuer = issuer
        # rate limit 버킷(임계값은 config). 로그인은 계정 버킷 + 클라이언트 버킷을 함께 적용 —
        # 같은 클라이언트가 사용자명을 바꿔가며 우회하는 것을 클라이언트 버킷이 막는다(§4).
        self.login_limiter = login_limiter or RateLimiter(
            max_attempts=config.login_max_per_account,
            window_seconds=config.login_rate_window_seconds,
        )
        self.login_client_limiter = login_client_limiter or RateLimiter(
            max_attempts=config.login_max_per_client,
            window_seconds=config.login_rate_window_seconds,
        )
        # 공개 가입은 요청마다 Argon2 해시 + insert — 사용자명 회전으로 CPU·저장공간을 소모시키지
        # 못하게 클라이언트(IP) 버킷으로 제한한다(§4).
        self.signup_limiter = signup_limiter or RateLimiter(
            max_attempts=config.signup_max_per_client,
            window_seconds=config.signup_rate_window_seconds,
        )
        self.exchange_limiter = exchange_limiter or RateLimiter(
            max_attempts=config.exchange_max_per_client,
            window_seconds=config.exchange_rate_window_seconds,
        )
        # 없는 사용자 로그인 시에도 동일한 argon2 검증 비용을 치르기 위한 실제 더미 해시(타이밍 평준화).
        self._dummy_hash = passwords.hash_password(uuid.uuid4().hex)

    def close(self) -> None:
        self.repo.close()

    # ── 회원가입 ───────────────────────────────────────────────────────
    def signup(
        self, username_raw: str, password: str, *, is_admin: bool = False, client_key: str = ""
    ) -> dict:
        """공개 회원가입 → 즉시 active member. 중복/정책 위반 시 예외.

        client_key(IP)가 주어지면 클라이언트 버킷 rate limit 을 **Argon2 해시 이전에** 적용해
        사용자명 회전으로 CPU·저장공간을 소모시키는 것을 막는다(§4). 오프라인 도구(client_key 미지정)는
        제한하지 않는다."""
        if not self.config.signup_enabled:
            raise AuthError("회원가입이 비활성화되어 있습니다.")
        if client_key and not self.signup_limiter.check(f"signup:{client_key}"):
            raise RateLimited("가입 시도가 너무 많습니다. 잠시 후 다시 시도하세요.")
        username = passwords.validate_username(username_raw)
        passwords.validate_password(password)
        now = _iso(_now())
        try:
            user = self.repo.create_user(
                username=username,
                password_hash=passwords.hash_password(password),
                is_admin=is_admin,
                status="active",
                now=now,
            )
        except UsernameTaken:
            raise
        self.repo.add_audit(event="signup", user_id=user["id"], now=now)
        return user

    # ── 로그인/세션 ────────────────────────────────────────────────────
    def _authenticate(self, username_raw: str, password: str) -> dict | None:
        """자격 검증 → user dict | None. 존재여부·상태 차이를 외부로 노출하지 않는다."""
        username = passwords.normalize_username(username_raw)
        user = self.repo.get_user_by_username(username)
        if user is None:
            # 타이밍 평준화 — 없는 사용자여도 실제 argon2 검증 비용을 치른다.
            passwords.verify_password(self._dummy_hash, password)
            return None
        # status 검사 **이전에** 항상 verify 를 수행해 응답 시간을 평준화한다 — 비활성 계정을
        # 즉시 반환하면 (없는 사용자·틀린 비번은 Argon2 비용을 치르므로) 응답 시간 차로 비활성
        # 계정을 열거할 수 있다. 존재/상태/비번 실패 모두 동일 메시지·유사 시간(§4).
        pw_ok = passwords.verify_password(user["password_hash"], password)
        if user["status"] != "active":
            return None
        if not pw_ok:
            return None
        if passwords.needs_rehash(user["password_hash"]):
            self.repo.update_user_password(
                user["id"], passwords.hash_password(password), _iso(_now())
            )
        return user

    def login(
        self, username_raw: str, password: str, *, client_key: str = ""
    ) -> tuple[str, dict, Principal]:
        """(session_token, session, principal). 실패는 AuthError(동일 메시지), 초과는 RateLimited.

        계정 버킷(login:acct:<username>) + 클라이언트 버킷(login:client:<ip>)을 함께 적용해
        둘 중 하나라도 초과하면 차단한다 — 계정 버킷만 있으면 같은 클라이언트가 사용자명을
        바꿔가며 우회할 수 있다(§4)."""
        account_key = f"login:acct:{passwords.normalize_username(username_raw)}"
        client_bucket = f"login:client:{client_key}"
        # 두 버킷 모두 기록·검사(둘 중 하나라도 초과 시 차단). 한쪽이 초과여도 나머지 검사는
        # 단락 평가로 생략 — 그 경우도 어차피 차단이므로 무방.
        if not self.login_limiter.check(account_key) or not self.login_client_limiter.check(
            client_bucket
        ):
            raise RateLimited("로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.")
        user = self._authenticate(username_raw, password)
        now_dt = _now()
        now = _iso(now_dt)
        if user is None:
            self.repo.add_audit(event="login_failed", now=now)
            raise AuthError(LOGIN_FAILED_MSG)
        token = tokens.new_session_token()
        session = self.repo.create_session(
            user_id=user["id"],
            token_hash=tokens.hash_session_token(token, self._session_secret),
            csrf_token=tokens.new_csrf_token(),
            expires_at=_iso(now_dt + datetime.timedelta(seconds=self.config.session_ttl_seconds)),
            now=now,
        )
        self.repo.add_audit(event="login", user_id=user["id"], now=now)
        return token, session, self.principal_for_user(user)

    def validate_session(self, token: str | None) -> tuple[dict, Principal] | None:
        """세션 토큰 → (session, principal). 폐기·만료·사용자 비활성이면 None."""
        if not token:
            return None
        th = tokens.hash_session_token(token, self._session_secret)
        session = self.repo.get_session_by_token_hash(th)
        if session is None or session.get("revoked_at"):
            return None
        exp = _parse(session.get("expires_at"))
        if exp is not None and exp < _now():
            return None
        user = self.repo.get_user_by_id(session["user_id"])
        if user is None or user["status"] != "active":
            return None
        self.repo.touch_session(session["id"], _iso(_now()))
        return session, self.principal_for_user(user)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        th = tokens.hash_session_token(token, self._session_secret)
        session = self.repo.get_session_by_token_hash(th)
        if session is not None:
            now = _iso(_now())
            self.repo.revoke_session(session["id"], now)
            self.repo.add_audit(event="logout", user_id=session["user_id"], now=now)

    def principal_for_user(self, user: dict) -> Principal:
        # 프로젝트 멤버십을 함께 로드(admin 은 전권이라 불필요). 웹은 매 요청 호출 → 즉시 반영.
        projects = {} if user["is_admin"] else self.repo.projects_for_user(user["id"])
        return Principal.for_user(
            user["username"],
            user_id=user["id"],
            is_admin=user["is_admin"],
            scopes=scopes_for(user["is_admin"]),
            projects=projects,
        )

    # ── 비밀번호 변경 (다른 세션 폐기, PAT 유지) ───────────────────────
    def change_password(
        self, user_id: str, old_password: str, new_password: str, *,
        current_session_id: str | None = None,
    ) -> None:
        user = self.repo.get_user_by_id(user_id)
        if user is None or not passwords.verify_password(user["password_hash"], old_password):
            raise AuthError("현재 비밀번호가 올바르지 않습니다.")
        passwords.validate_password(new_password)
        now = _iso(_now())
        self.repo.update_user_password(user_id, passwords.hash_password(new_password), now)
        # 정책: 현재 세션 유지, 다른 모든 웹 세션 폐기. PAT 는 유지(§3).
        self.repo.revoke_sessions_for_user(user_id, now, except_session_id=current_session_id)
        self.repo.add_audit(event="password_changed", user_id=user_id, now=now)

    # ── PAT ────────────────────────────────────────────────────────────
    def create_pat(
        self, user: dict, *, name: str, scopes: list[str],
        expires_at: str | None = None,
    ) -> tuple[str, dict]:
        """(full_token, pat_view). 원문은 여기서만 반환(1회 표시). scope 상승은 거부."""
        allowed = set(scopes_for(user["is_admin"]))
        requested = [s for s in scopes if s]
        if not requested:
            requested = list(allowed)
        if not set(requested) <= allowed:
            raise PermissionError("보유 권한을 초과하는 scope 는 PAT 에 지정할 수 없습니다.")
        token_id = uuid.uuid4().hex
        full, prefix, secret = tokens.new_pat(token_id)
        now = _iso(_now())
        pat = self.repo.create_pat(
            token_id=token_id,
            user_id=user["id"],
            name=(name or "token").strip()[:80],
            prefix=prefix,
            secret_hash=tokens.hash_pat_secret(secret, self._pepper),
            scopes=requested,
            expires_at=expires_at,
            now=now,
        )
        self.repo.add_audit(event="pat_created", user_id=user["id"], meta=prefix, now=now)
        pat.pop("secret_hash", None)
        return full, pat

    def list_pats(self, user_id: str) -> list[dict]:
        return self.repo.list_pats(user_id)

    def revoke_pat(self, pat_id: str, user_id: str) -> bool:
        now = _iso(_now())
        ok = self.repo.revoke_pat(pat_id, user_id, now)
        if ok:
            self.repo.add_audit(event="pat_revoked", user_id=user_id, meta=pat_id, now=now)
        return ok

    # ── 프로젝트 관리 (admin 전용 — 인가는 인터페이스에서 review scope 로 강제) ──
    def ensure_default_project(self, slug: str) -> None:
        self.repo.ensure_project(slug, slug, _iso(_now()))

    def list_all_projects(self) -> list[dict]:
        return self.repo.list_all_projects()

    def get_project(self, slug: str) -> dict | None:
        return self.repo.get_project(slug)

    def create_project(self, slug: str, name: str, description: str | None,
                       *, created_by: str | None) -> dict:
        slug = normalize_project_slug(slug)
        now = _iso(_now())
        p = self.repo.create_project(
            slug=slug, name=(name or slug).strip(), description=(description or None),
            created_by=created_by, now=now,
        )
        self.repo.add_audit(event="project_created", user_id=created_by, meta=slug, now=now)
        return p

    def update_project(self, slug: str, name: str, description: str | None) -> None:
        self.repo.update_project(slug, name=(name or slug).strip(),
                                 description=(description or None))

    def delete_project(self, slug: str, *, actor_id: str | None = None) -> None:
        """프로젝트·멤버십 삭제(접근권한 회수). 지식 문서는 그대로 남는다(admin 만 접근)."""
        now = _iso(_now())
        self.repo.delete_project(slug)
        self.repo.add_audit(event="project_deleted", user_id=actor_id, meta=slug, now=now)

    def project_members(self, slug: str) -> list[dict]:
        return self.repo.list_members(slug)

    def add_project_member(self, slug: str, user_id: str, role: str,
                           *, actor_id: str | None = None) -> None:
        if role not in PROJECT_ROLES:
            raise ValueError(f"역할은 {PROJECT_ROLES} 중 하나여야 합니다.")
        now = _iso(_now())
        self.repo.add_member(slug, user_id, role, now)
        self.repo.add_audit(event="project_member_added", user_id=actor_id,
                            meta=f"{slug}:{user_id}:{role}", now=now)

    def remove_project_member(self, slug: str, user_id: str,
                              *, actor_id: str | None = None) -> None:
        now = _iso(_now())
        self.repo.remove_member(slug, user_id)
        self.repo.add_audit(event="project_member_removed", user_id=actor_id,
                            meta=f"{slug}:{user_id}", now=now)

    def list_users(self) -> list[dict]:
        return self.repo.list_users()

    # ── PAT → 단기 JWT 교환 ────────────────────────────────────────────
    def exchange_pat_for_jwt(self, pat_token: str, *, client_key: str = "") -> dict:
        """유효 PAT → access token(JWT). 응답: {access_token, token_type, expires_in, scope}."""
        if self.issuer is None:
            raise AuthError("JWT 발급이 구성되지 않았습니다.")
        if not self.exchange_limiter.check(f"exchange:{client_key}"):
            raise RateLimited("토큰 교환 시도가 너무 많습니다.")
        parsed = tokens.parse_pat(pat_token or "")
        if parsed is None:
            raise AuthError("유효하지 않은 PAT 입니다.")
        token_id, secret = parsed
        pat = self.repo.get_pat(token_id)
        now_dt = _now()
        if (
            pat is None
            or not tokens.pat_secret_matches(secret, self._pepper, pat["secret_hash"])
            or pat.get("revoked_at")
        ):
            raise AuthError("유효하지 않은 PAT 입니다.")
        exp = _parse(pat.get("expires_at"))
        if exp is not None and exp < now_dt:
            raise AuthError("만료된 PAT 입니다.")
        user = self.repo.get_user_by_id(pat["user_id"])
        if user is None or user["status"] != "active":
            raise AuthError("유효하지 않은 PAT 입니다.")
        # 발급 이후 권한 강등 대비 — 현재 사용자 scope 와 교집합.
        eff = [s for s in pat["scopes"] if s in set(scopes_for(user["is_admin"]))]
        # 프로젝트 멤버십을 클레임에 포함(admin 은 전권이라 생략). 발급 시점 스냅샷.
        projects = {} if user["is_admin"] else self.repo.projects_for_user(user["id"])
        token, ttl = self.issuer.issue(
            subject=user["id"],
            username=user["username"],
            is_admin=user["is_admin"],
            scopes=eff,
            projects=projects,
            now=int(now_dt.timestamp()),
        )
        self.repo.touch_pat(pat["id"], _iso(now_dt))
        self.repo.add_audit(event="pat_exchanged", user_id=user["id"], meta=pat["prefix"], now=_iso(now_dt))
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": " ".join(eff),
        }


# ── 팩토리 (admin 서버용 — private key 보유) ────────────────────────────
def _read_file(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


def build_auth_repository(config: Config, root) -> AuthRepository:
    """지식 백엔드에 맞춰 auth 저장소 선택 (file→SQLite, postgres→Postgres)."""
    if config.storage == "postgres":
        return PostgresAuthRepository(config.postgres.resolve_dsn())
    return SQLiteAuthRepository(Path(root) / "auth.sqlite")


def build_auth_service(config: Config, root) -> AuthService:
    """admin 서버용 AuthService 구성 — env/파일에서 secret·키 주입. auth 비활성이면 호출하지 않음.

    필수 env: AUTH_PAT_PEPPER, AUTH_SESSION_SECRET, AUTH_PRIVATE_KEY_FILE, AUTH_PUBLIC_KEY_FILE.
    """
    ac = config.auth
    pepper = os.environ.get(ac.pat_pepper_env)
    session_secret = os.environ.get(ac.session_secret_env)
    if not pepper or not session_secret:
        raise RuntimeError(
            f"인증 활성 — {ac.pat_pepper_env}·{ac.session_secret_env} 환경변수가 필요합니다."
        )
    priv = _read_file(ac.private_key_file)
    pub = _read_file(ac.public_key_file)
    if not priv or not pub:
        raise RuntimeError(
            "인증 활성 — AUTH_PRIVATE_KEY_FILE·AUTH_PUBLIC_KEY_FILE 가 필요합니다."
        )
    issuer = JwtIssuer(
        private_key_pem=priv,
        public_key_pem=pub,
        issuer=ac.issuer,
        audience=ac.mcp_audience,
        ttl_seconds=ac.access_token_ttl_seconds,
    )
    repo = build_auth_repository(config, root)
    svc = AuthService(repo, ac, pat_pepper=pepper, session_secret=session_secret, issuer=issuer)
    svc.ensure_default_project(config.default_project)  # 기본 프로젝트를 관리 목록에 노출
    return svc
