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


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a stored hash in constant time.

    Supports both the current self-describing format and the legacy
    ``salt:hash`` form (assumed to be 100k iterations).
    """
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
