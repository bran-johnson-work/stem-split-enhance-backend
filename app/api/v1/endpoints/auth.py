import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_token_payload
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import create_access_token
from app.schemas.user import Token, UserCreate, UserResponse
from app.services.auth_service import auth_service
from app.services.user_service import user_service

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)

# Per-process login throttle keyed by client IP + email. Back with a shared
# store (e.g. Redis) for multi-instance deployments.
login_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.login_rate_limit_max_attempts,
    window_seconds=settings.login_rate_limit_window_seconds,
)

# Generic, non-enumerable client-facing message. The real reason is logged.
_INVALID_CREDENTIALS = "Incorrect email or password"


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate, db: AsyncSession = Depends(get_db)) -> Any:
    """Register a new user."""
    try:
        user = await user_service.create_user(db, user_in)
    except ValueError as exc:
        logger.info("Registration rejected for email=%s: %s", user_in.email, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to register with the provided details.",
        )
    logger.info("Registered user_id=%s", user.id)
    return user


@router.post("/login", response_model=Token)
async def login(
    request: Request,
    db: AsyncSession = Depends(get_db),
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> Any:
    """OAuth2-compatible token login."""
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{client_ip}:{form_data.username.lower()}"

    if login_rate_limiter.is_blocked(rate_key):
        logger.warning("Login rate limit exceeded for %s", rate_key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
        )

    user = await user_service.authenticate(
        db, email=form_data.username, password=form_data.password
    )
    # Collapse "no such user", "wrong password", and "inactive" into one response
    # so the endpoint cannot be used to enumerate accounts.
    if user is None or not user.is_active:
        login_rate_limiter.register_failure(rate_key)
        reason = "invalid_credentials" if user is None else "inactive_user"
        logger.info("Failed login email=%s reason=%s", form_data.username, reason)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
            headers={"WWW-Authenticate": "Bearer"},
        )

    login_rate_limiter.reset(rate_key)
    logger.info("Successful login user_id=%s", user.id)
    return Token(
        access_token=create_access_token(data={"sub": str(user.id)}),
        token_type="bearer",
    )


@router.post("/logout")
async def logout(
    payload: dict = Depends(get_token_payload),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Revoke the presented access token (logout)."""
    jti = payload.get("jti")
    exp = payload.get("exp")
    if jti and exp:
        await auth_service.logout(db, jti=jti, exp_timestamp=int(exp))
    return {"detail": "Successfully logged out"}
