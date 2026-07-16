"""비밀번호 해시(Argon2id) + 사용자명/비번 정책 (구현스펙-인증인가-RBAC.md §4).

평문 저장 금지. 로그인 실패 메시지는 호출측이 사용자 존재여부가 새지 않도록 동일하게 처리한다.
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LEN = 10

# 3~32자 · 영소문자로 시작 · 영소문자/숫자/._- (정규화 후 검증 → unique 안정).
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{2,31}$")

_ph = PasswordHasher()  # Argon2id (argon2-cffi 기본 파라미터)


class PasswordPolicyError(ValueError):
    """사용자명/비밀번호 정책 위반(회원가입·비번변경 입력 검증)."""


def normalize_username(raw: str) -> str:
    """대소문자·주변 공백 차이를 없앤 정규형(unique 판정 기준)."""
    return (raw or "").strip().lower()


def validate_username(raw: str) -> str:
    """정규화 + 형식 검증. 통과 시 정규화된 username 반환, 아니면 PasswordPolicyError."""
    u = normalize_username(raw)
    if not _USERNAME_RE.match(u):
        raise PasswordPolicyError(
            "사용자명은 3~32자, 영소문자로 시작하고 영소문자/숫자/._- 만 쓸 수 있습니다."
        )
    return u


def validate_password(pw: str) -> None:
    """최소 비밀번호 정책. 위반 시 PasswordPolicyError."""
    if not pw or len(pw) < MIN_PASSWORD_LEN:
        raise PasswordPolicyError(f"비밀번호는 최소 {MIN_PASSWORD_LEN}자 이상이어야 합니다.")


def hash_password(pw: str) -> str:
    """Argon2id 해시 문자열(파라미터 포함). 원문은 저장하지 않는다."""
    return _ph.hash(pw)


def verify_password(stored_hash: str, pw: str) -> bool:
    """해시와 평문 대조. 불일치·손상 해시는 False(예외 누출 금지 — 타이밍은 argon2 가 처리)."""
    try:
        return _ph.verify(stored_hash, pw)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """파라미터가 갱신됐는지(로그인 성공 시 재해시 판단)."""
    try:
        return _ph.check_needs_rehash(stored_hash)
    except (InvalidHashError, Exception):  # noqa: BLE001 - 손상 해시는 재해시 대상
        return True
