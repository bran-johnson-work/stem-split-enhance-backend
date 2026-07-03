from datetime import timedelta

import jwt
import pytest

from app.core import security
from app.core.config import get_settings


def test_hash_password_uses_self_describing_format():
    hashed = security.hash_password("secret")
    scheme, iterations, salt_hex, digest_hex = hashed.split("$")
    assert scheme == "pbkdf2_sha256"
    assert int(iterations) == get_settings().pbkdf2_iterations
    assert len(bytes.fromhex(salt_hex)) == 16
    assert digest_hex


def test_hash_password_uses_unique_salt():
    assert security.hash_password("secret") != security.hash_password("secret")


def test_verify_password_accepts_correct():
    assert security.verify_password("secret", security.hash_password("secret")) is True


def test_verify_password_rejects_wrong():
    assert security.verify_password("wrong", security.hash_password("secret")) is False


def test_verify_password_rejects_malformed_hash():
    assert security.verify_password("secret", "garbage") is False


def test_verify_password_supports_legacy_format():
    # Legacy "salt:hash" hashes (100k iterations) must still verify.
    import hashlib
    import os

    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", b"legacypw", salt, 100_000)
    legacy = f"{salt.hex()}:{digest.hex()}"
    assert security.verify_password("legacypw", legacy) is True
    assert security.verify_password("nope", legacy) is False


def test_access_token_has_full_claim_set():
    token = security.create_access_token({"sub": "user-123"})
    payload = security.decode_access_token(token)
    settings = get_settings()
    assert payload["sub"] == "user-123"
    assert payload["iss"] == settings.jwt_issuer
    assert payload["aud"] == settings.jwt_audience
    for claim in ("iat", "nbf", "exp", "jti"):
        assert claim in payload


def test_each_token_has_unique_jti():
    a = security.decode_access_token(security.create_access_token({"sub": "x"}))
    b = security.decode_access_token(security.create_access_token({"sub": "x"}))
    assert a["jti"] != b["jti"]


def test_decode_rejects_wrong_audience():
    from datetime import datetime, timezone
    from uuid import uuid4

    settings = get_settings()
    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {
            "sub": "x",
            "aud": "someone-else",  # wrong audience
            "iss": settings.jwt_issuer,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.InvalidAudienceError):
        security.decode_access_token(forged)


def test_decode_rejects_expired_token():
    token = security.create_access_token({"sub": "x"}, expires_delta=timedelta(seconds=-1))
    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_access_token(token)


def test_expire_minutes_is_config_driven():
    settings = get_settings()
    payload = security.decode_access_token(security.create_access_token({"sub": "x"}))
    span_minutes = (payload["exp"] - payload["iat"]) / 60
    assert round(span_minutes) == settings.access_token_expire_minutes
