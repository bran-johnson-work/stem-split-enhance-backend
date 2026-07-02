from datetime import timedelta

import jwt

from app.core import security
from app.core.config import get_settings


def test_hash_password_format():
    hashed = security.hash_password("secret")
    salt_hex, digest_hex = hashed.split(":")
    assert len(bytes.fromhex(salt_hex)) == 16  # 16-byte random salt
    assert digest_hex


def test_hash_password_uses_unique_salt():
    # Same input hashed twice must differ because the salt is random.
    assert security.hash_password("secret") != security.hash_password("secret")


def test_verify_password_accepts_correct():
    hashed = security.hash_password("secret")
    assert security.verify_password("secret", hashed) is True


def test_verify_password_rejects_wrong():
    hashed = security.hash_password("secret")
    assert security.verify_password("wrong", hashed) is False


def test_verify_password_rejects_malformed_hash():
    assert security.verify_password("secret", "not-a-valid-hash") is False


def test_create_access_token_roundtrips():
    settings = get_settings()
    token = security.create_access_token({"sub": "user-123"})
    payload = jwt.decode(token, settings.secret_key, algorithms=[security.ALGORITHM])
    assert payload["sub"] == "user-123"
    assert "exp" in payload


def test_create_access_token_honours_custom_expiry():
    settings = get_settings()
    short = security.create_access_token({"sub": "x"}, expires_delta=timedelta(seconds=1))
    long = security.create_access_token({"sub": "x"}, expires_delta=timedelta(days=1))
    short_exp = jwt.decode(short, settings.secret_key, algorithms=[security.ALGORITHM])["exp"]
    long_exp = jwt.decode(long, settings.secret_key, algorithms=[security.ALGORITHM])["exp"]
    assert long_exp > short_exp


def test_expired_token_raises_on_decode():
    settings = get_settings()
    token = security.create_access_token({"sub": "x"}, expires_delta=timedelta(seconds=-1))
    try:
        jwt.decode(token, settings.secret_key, algorithms=[security.ALGORITHM])
        raise AssertionError("expected ExpiredSignatureError")
    except jwt.ExpiredSignatureError:
        pass
