import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User, UserRole
from app.repositories.revoked_token_repository import revoked_token_repository
from app.repositories.user_repository import user_repository

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

credentials_exception = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_token_payload(token: str = Depends(oauth2_scheme)) -> dict[str, Any]:
    """Decode and fully validate the bearer token, returning its claims."""
    try:
        return decode_access_token(token)
    except jwt.PyJWTError as exc:
        logger.info("Rejected JWT: %s", exc)
        raise credentials_exception


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    payload: dict[str, Any] = Depends(get_token_payload),
) -> User:
    """Resolve the authenticated user, rejecting revoked (logged-out) tokens."""
    jti = payload.get("jti")
    if jti and await revoked_token_repository.is_revoked(db, jti):
        logger.info("Rejected revoked token jti=%s", jti)
        raise credentials_exception

    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except ValueError:
        raise credentials_exception

    user = await user_repository.get(db, user_id)
    if user is None:
        raise credentials_exception
    return user


def require_role(*roles: UserRole) -> Callable[[User], Awaitable[User]]:
    """Build a dependency that allows only the given role(s).

    Reuses ``get_current_user`` (no duplicate JWT parsing / revocation checks) and
    raises 403 if the authenticated user's role is not in ``roles``. Returns the
    user so guarded routes can still receive ``current_user``.
    """

    async def _guard(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            logger.info(
                "Forbidden: user_id=%s role=%s not in %s",
                current_user.id,
                current_user.role,
                [r.value for r in roles],
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user

    return _guard
