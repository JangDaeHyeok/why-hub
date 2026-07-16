"""인증 — 비밀번호 해시·정책 (구현스펙-인증인가-RBAC.md §4)."""

from __future__ import annotations

import pytest

from hub.auth import passwords


def test_hash_is_argon2id_and_not_plaintext():
    h = passwords.hash_password("supersecret123")
    assert h.startswith("$argon2id$")
    assert "supersecret123" not in h


def test_verify_true_false():
    h = passwords.hash_password("supersecret123")
    assert passwords.verify_password(h, "supersecret123") is True
    assert passwords.verify_password(h, "wrong") is False
    assert passwords.verify_password("not-a-hash", "x") is False


def test_username_normalize_and_validate():
    assert passwords.normalize_username("  Alice ") == "alice"
    assert passwords.validate_username("Bob_1") == "bob_1"
    for bad in ("", "ab", "1abc", "has space", "x" * 40, "汉字"):
        with pytest.raises(passwords.PasswordPolicyError):
            passwords.validate_username(bad)


def test_password_policy_min_length():
    passwords.validate_password("0123456789")  # 10자 OK
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate_password("short")
