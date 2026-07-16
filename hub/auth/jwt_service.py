"""JWT 발급(RS256) + JWKS — admin 서버 전용 (구현스펙-인증인가-RBAC.md §6).

private key 는 admin 서버만 보유하고 PAT→JWT 를 발급한다. MCP 서버는 public key/JWKS 로
stateless 검증만 한다(이 모듈을 쓰지 않음). 알고리즘은 RS256 고정 — 토큰 header 값으로 임의
선택하지 않는다(검증측 JWTVerifier 도 algorithm 을 고정).
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_rsa_keypair(bits: int = 2048) -> tuple[str, str]:
    """(private_pem, public_pem) 생성 — 로컬/테스트 키 발급용(운영은 openssl 로 외부 생성 권장)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    pub = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return priv, pub


def kid_for(public_key_pem: str) -> str:
    """public key 로부터 안정적인 kid(키 로테이션 대비). SHA-256 앞 16자리."""
    return hashlib.sha256(public_key_pem.encode("utf-8")).hexdigest()[:16]


def _b64u(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


class JwtIssuer:
    """PAT 검증 성공 후 단기 access token(JWT)을 RS256 으로 발급."""

    def __init__(
        self,
        *,
        private_key_pem: str,
        public_key_pem: str,
        issuer: str,
        audience: str,
        kid: str | None = None,
        ttl_seconds: int = 600,
        algorithm: str = "RS256",
    ):
        self.private_key_pem = private_key_pem
        self.public_key_pem = public_key_pem
        self.issuer = issuer
        self.audience = audience
        self.kid = kid or kid_for(public_key_pem)
        self.ttl_seconds = ttl_seconds
        self.algorithm = algorithm

    def issue(
        self,
        *,
        subject: str,
        username: str,
        is_admin: bool,
        scopes: list[str] | tuple[str, ...],
        projects: dict[str, str] | None = None,
        now: int | None = None,
    ) -> tuple[str, int]:
        """access token(JWT) 발급 → (token, expires_in). scope 가 최종 권한 판정 기준.

        projects: 프로젝트별 역할 {slug: role}(멤버십 변경은 이 토큰 만료 후 반영 — staleness ≤ TTL).
        """
        iat = int(now if now is not None else time.time())
        payload = {
            "iss": self.issuer,
            "sub": subject,
            "aud": self.audience,
            "iat": iat,
            "nbf": iat,
            "exp": iat + self.ttl_seconds,
            "jti": uuid.uuid4().hex,
            "scope": " ".join(scopes),
            "projects": projects or {},  # 프로젝트 ACL 클레임(MCP 무상태 검증용)
            "username": username,  # 표시용
            "is_admin": bool(is_admin),  # 표시용
        }
        token = jwt.encode(
            payload, self.private_key_pem, algorithm=self.algorithm, headers={"kid": self.kid}
        )
        return token, self.ttl_seconds

    def public_jwk(self) -> dict:
        pub = serialization.load_pem_public_key(self.public_key_pem.encode("utf-8"))
        numbers = pub.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": self.algorithm,
            "kid": self.kid,
            "n": _b64u(numbers.n),
            "e": _b64u(numbers.e),
        }

    def jwks(self) -> dict:
        return {"keys": [self.public_jwk()]}
