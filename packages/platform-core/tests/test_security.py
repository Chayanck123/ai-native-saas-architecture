from __future__ import annotations

import time

import jwt
import pytest

from platform_core.errors import AuthenticationRequired, ValidationFailed
from platform_core.rbac import Permission, Role
from platform_core.security import (
    SignatureInvalid,
    TokenService,
    generate_api_key,
    hash_api_key,
    hash_password,
    parse_api_key,
    sign_payload,
    verify_password,
    verify_payload_signature,
)
from platform_core.testing import tenant_principal

SECRET = "x" * 40


def test_password_hash_roundtrip() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong password!!", stored)
    # Salted: the same password hashes differently each time.
    assert hash_password("correct horse battery") != stored


def test_short_password_rejected() -> None:
    with pytest.raises(ValidationFailed):
        hash_password("short")


def test_api_key_generation_and_parsing() -> None:
    plaintext, key_id, key_hash = generate_api_key()
    assert plaintext.startswith(f"ank_{key_id}_")
    assert parse_api_key(plaintext) == key_id
    assert hash_api_key(plaintext) == key_hash
    assert parse_api_key("ank_bad") is None
    assert parse_api_key("sk_live_whatever_" + "a" * 48) is None


def test_jwt_roundtrip_preserves_principal() -> None:
    tokens = TokenService(SECRET, issuer="iss", audience="aud")
    principal = tenant_principal("ten_1", role=Role.ADMIN, plan="pro")
    decoded = tokens.verify(tokens.issue(principal))
    assert decoded.tenant_id == "ten_1"
    assert decoded.role is Role.ADMIN
    assert decoded.plan == "pro"
    assert Permission.MEMBERS_MANAGE in decoded.permissions
    assert Permission.BILLING_MANAGE not in decoded.permissions


def test_jwt_rejects_wrong_audience_tampering_and_expiry() -> None:
    tokens = TokenService(SECRET, issuer="iss", audience="aud")
    other = TokenService(SECRET, issuer="iss", audience="other")
    token = tokens.issue(tenant_principal())
    with pytest.raises(AuthenticationRequired):
        other.verify(token)
    with pytest.raises(AuthenticationRequired):
        tokens.verify(token[:-4] + "abcd")
    expired = jwt.encode(
        {"iss": "iss", "aud": "aud", "sub": "u", "iat": 1, "exp": int(time.time()) - 60},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationRequired) as excinfo:
        tokens.verify(expired)
    assert excinfo.value.code == "token_expired"


def test_payload_signature_verification() -> None:
    body = b'{"id":"evt_1"}'
    header = sign_payload("whsec_test", body, timestamp=1_700_000_000)
    assert verify_payload_signature(header, "whsec_test", body, now=1_700_000_100) == 1_700_000_000
    with pytest.raises(SignatureInvalid):
        verify_payload_signature(header, "whsec_other", body, now=1_700_000_100)
    with pytest.raises(SignatureInvalid):
        verify_payload_signature(header, "whsec_test", body + b" ", now=1_700_000_100)
    with pytest.raises(SignatureInvalid, match="tolerance"):
        verify_payload_signature(header, "whsec_test", body, now=1_700_009_999)
    with pytest.raises(SignatureInvalid):
        verify_payload_signature("garbage", "whsec_test", body)
