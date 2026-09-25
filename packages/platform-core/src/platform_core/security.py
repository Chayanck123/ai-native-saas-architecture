"""Credentials and signatures: passwords, API keys, JWTs and HMAC webhook signatures."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any

import jwt

from platform_core.context import Principal, PrincipalKind
from platform_core.errors import AuthenticationRequired, ValidationFailed
from platform_core.ids import new_id
from platform_core.rbac import Permission, Role, parse_permissions

# --------------------------------------------------------------------------- #
# Passwords (scrypt: memory-hard, in the standard library)
# --------------------------------------------------------------------------- #
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValidationFailed("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
    )
    return hmac.compare_digest(candidate, base64.b64decode(digest_b64))


# --------------------------------------------------------------------------- #
# API keys: ank_<key_id>_<secret>. Only a SHA-256 of the full key is stored.
# (A fast hash is fine here: keys carry 192 bits of entropy, unlike passwords.)
# --------------------------------------------------------------------------- #
API_KEY_PREFIX = "ank"


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, key_id, key_hash)``. The plaintext is shown to the user once."""
    key_id = new_id()[-12:]
    plaintext = f"{API_KEY_PREFIX}_{key_id}_{secrets.token_hex(24)}"
    return plaintext, key_id, hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def parse_api_key(plaintext: str) -> str | None:
    """Return the key id embedded in an API key, or ``None`` if it is malformed."""
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX or not parts[1] or len(parts[2]) != 48:
        return None
    return parts[1]


# --------------------------------------------------------------------------- #
# JWTs
# --------------------------------------------------------------------------- #
class TokenService:
    """Issues and verifies HS256 JWTs carrying a :class:`Principal`.

    Two instances exist in practice:

    * **user tokens** - issued by the gateway to browsers/SDK clients;
    * **internal tokens** - short-lived tokens the gateway mints for each proxied
      request (and services mint for service-to-service calls). Internal services
      trust *only* internal tokens, so a leaked user token cannot call them directly.

    In production you would switch to asymmetric keys (RS256/EdDSA) so services
    only hold a public key; the interface stays the same.
    """

    def __init__(self, secret: str, *, issuer: str, audience: str, algorithm: str = "HS256"):
        if len(secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._algorithm = algorithm

    def issue(self, principal: Principal, ttl_seconds: int = 3600, **extra: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": principal.subject,
            "kind": principal.kind.value,
            "tid": principal.tenant_id,
            "role": principal.role.value,
            "perms": sorted(p.value for p in principal.permissions),
            "plan": principal.plan,
            "staff": principal.is_staff,
            "email": principal.email,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": new_id(),
            **extra,
        }
        return jwt.encode(claims, self._secret, algorithm=self._algorithm)

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                leeway=10,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationRequired("token expired", code="token_expired") from exc
        except jwt.PyJWTError as exc:
            raise AuthenticationRequired("invalid token", code="invalid_token") from exc
        return Principal(
            subject=claims["sub"],
            kind=PrincipalKind(claims.get("kind", "user")),
            tenant_id=claims.get("tid"),
            role=Role(claims.get("role", Role.VIEWER.value)),
            permissions=parse_permissions(claims.get("perms", [])),
            plan=claims.get("plan", "free"),
            email=claims.get("email"),
            is_staff=bool(claims.get("staff", False)),
        )


INTERNAL_ISSUER = "ainative-internal"
INTERNAL_AUDIENCE = "ainative-services"
INTERNAL_TOKEN_HEADER = "x-internal-token"  # noqa: S105 - a header name


def internal_token_service(secret: str) -> TokenService:
    return TokenService(secret, issuer=INTERNAL_ISSUER, audience=INTERNAL_AUDIENCE)


# --------------------------------------------------------------------------- #
# Timestamped HMAC signatures (the same scheme Stripe uses: "t=<ts>,v1=<hex>")
# --------------------------------------------------------------------------- #
class SignatureInvalid(Exception):
    pass


def sign_payload(secret: str, payload: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_payload_signature(
    header: str,
    secret: str,
    payload: bytes,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> int:
    """Verify a ``t=...,v1=...`` signature header; return the signed timestamp.

    Rejects stale timestamps to stop replay attacks and uses a constant-time
    comparison to avoid timing side channels.
    """
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureInvalid("malformed timestamp") from exc
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise SignatureInvalid("missing timestamp or signature")
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise SignatureInvalid("timestamp outside tolerance")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureInvalid("signature mismatch")
    return timestamp


def all_permissions() -> frozenset[Permission]:
    return frozenset(Permission)
