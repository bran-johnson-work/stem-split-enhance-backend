import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt

from app.core.config import get_settings

settings = get_settings()

# Self-describing PBKDF2 hash format: "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>".
# Encoding the iteration count lets us raise the work factor without invalidating
# existing hashes. Legacy hashes use the "<salt_hex>:<hash_hex>" form at 100k iterations.
_HASH_PREFIX = "pbkdf2_sha256"
_LEGACY_ITERATIONS = 100_000


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 using a unique random salt."""
    iterations = settings.pbkdf2_iterations
    salt = os.urandom(16)
    digest = _pbkdf2(password, salt, iterations)
    return f"{_HASH_PREFIX}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """Verify a password against a stored hash in constant time.

    Supports both the current self-describing format and the legacy
    ``salt:hash`` form (assumed to be 100k iterations). Returns ``False`` for a
    missing/empty stored hash, so passwordless (OAuth2) accounts can never be
    authenticated via a password — including an empty one.
    """
    if not hashed_password:
        return False
    try:
        if hashed_password.startswith(f"{_HASH_PREFIX}$"):
            _, iter_str, salt_hex, hash_hex = hashed_password.split("$")
            iterations = int(iter_str)
        elif ":" in hashed_password:
            salt_hex, hash_hex = hashed_password.split(":")
            iterations = _LEGACY_ITERATIONS
        else:
            return False
        expected = bytes.fromhex(hash_hex)
        candidate = _pbkdf2(plain_password, bytes.fromhex(salt_hex), iterations)
        return hmac.compare_digest(candidate, expected)
    except (ValueError, AttributeError):
        return False


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT access token with a full, verifiable claim set."""
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode = {
        **data,
        "iat": now,
        "nbf": now,
        "exp": expire,
        "jti": str(uuid4()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and fully validate a JWT (signature, exp/nbf, issuer, audience).

    Raises ``jwt.PyJWTError`` on any validation failure.
    """
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "sub", "jti"]},
    )


# Single-purpose token type carried in the ``purpose`` claim. Verification links
# reuse the JWT machinery (signed, expiring, self-contained) so no extra table is
# needed. Two independent guards stop these tokens being confused with API access
# tokens: a distinct ``aud`` (rejected by ``decode_access_token``'s audience
# check) and this ``purpose`` claim (checked below). Either alone is sufficient.
EMAIL_VERIFICATION_PURPOSE = "email_verification"


def create_email_verification_token(user_id: str, email: str) -> str:
    """Create a short-lived signed token proving ownership of ``email``.

    Expires after ``email_verification_token_expire_hours`` (24h by default) and
    is scoped to the email-verification audience so it cannot be replayed as an
    API access token.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=settings.email_verification_token_expire_hours)
    to_encode = {
        "sub": user_id,
        "email": email,
        "purpose": EMAIL_VERIFICATION_PURPOSE,
        "iat": now,
        "nbf": now,
        "exp": expire,
        "jti": str(uuid4()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_email_verification_audience,
    }
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_email_verification_token(token: str) -> dict[str, Any]:
    """Decode and validate an email-verification token.

    Enforces signature, expiry (``exp``), not-before, issuer and the dedicated
    email-verification audience. Raises ``jwt.PyJWTError`` on any validation
    failure, including a token whose ``purpose`` claim is not the
    email-verification purpose (e.g. an access token presented here).
    """
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_email_verification_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "sub", "jti"]},
    )
    if payload.get("purpose") != EMAIL_VERIFICATION_PURPOSE:
        raise jwt.InvalidTokenError("Token is not an email-verification token")
    return payload
