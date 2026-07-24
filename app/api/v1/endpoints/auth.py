import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_token_payload
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import create_access_token
from app.schemas.email import MessageResponse, VerificationRequest, VerifyEmailRequest
from app.schemas.user import (
    LogoutRequest,
    RefreshRequest,
    Token,
    UserCreate,
    UserResponse,
)
from app.services.auth_service import RefreshError, auth_service
from app.services.email_service import email_service
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

# Per-IP throttle for the unauthenticated, email-triggering endpoints
# (register, verify-email/request) to curb signup spam / email bombing.
signup_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.signup_rate_limit_max_attempts,
    window_seconds=settings.signup_rate_limit_window_seconds,
)

# Generic, non-enumerable client-facing message. The real reason is logged.
_INVALID_CREDENTIALS = "Incorrect email or password"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_signup_rate_limit(request: Request) -> None:
    """Throttle an email-triggering endpoint per client IP; 429 when exceeded."""
    key = _client_ip(request)
    if signup_rate_limiter.is_blocked(key):
        logger.warning("Signup/verification rate limit exceeded for %s", key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
        )
    signup_rate_limiter.register_failure(key)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    user_in: UserCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Register a new user and send a verification email."""
    _enforce_signup_rate_limit(request)
    try:
        user = await user_service.create_user(db, user_in)
    except ValueError as exc:
        logger.info("Registration rejected for email=%s: %s", user_in.email, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to register with the provided details.",
        )
    logger.info("Registered user_id=%s", user.id)
    # Fire-and-forget: a failed email must not fail registration.
    background_tasks.add_task(email_service.send_verification_email, user)
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

    # Credentials are valid at this point, so gating on verification status here
    # (rather than collapsing it into the generic 401 above) is not an account-
    # enumeration oracle: only someone who already knows the password sees it.
    if settings.require_email_verification and not user.is_verified:
        logger.info("Login blocked for unverified user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email address before logging in.",
        )

    logger.info("Successful login user_id=%s", user.id)
    refresh_token = await auth_service.issue_refresh_token(db, user.id)
    await db.commit()
    return Token(
        access_token=create_access_token(data={"sub": str(user.id)}),
        token_type="bearer",
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/refresh", response_model=Token)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Exchange a refresh token for a new access token.

    This is what keeps a user signed in: the access token stays deliberately
    short-lived, and the client trades this credential for a new one in the
    background rather than making the user log in again every 30 minutes.

    The refresh token is single-use — a new one comes back with each call, and
    the presented one is retired. Clients must store the replacement.
    """
    try:
        user, new_refresh_token = await auth_service.rotate_refresh_token(
            db, body.refresh_token
        )
    except RefreshError as exc:
        # Every failure mode collapses to one response: a client can only act on
        # "log in again" regardless of which it was.
        logger.info("Refresh rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return Token(
        access_token=create_access_token(data={"sub": str(user.id)}),
        token_type="bearer",
        refresh_token=new_refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/logout")
async def logout(
    body: LogoutRequest | None = None,
    payload: dict = Depends(get_token_payload),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Revoke the presented access token, and the refresh token if one is sent.

    Both matter: denylisting the access token alone would leave the refresh
    token live, and the client could mint a new session straight after logging
    out. The body is optional so an older client still logs out cleanly.
    """
    jti = payload.get("jti")
    exp = payload.get("exp")
    if jti and exp:
        await auth_service.logout(db, jti=jti, exp_timestamp=int(exp))
    if body and body.refresh_token:
        await auth_service.revoke_refresh_token(db, body.refresh_token)
    return {"detail": "Successfully logged out"}


@router.post("/verify-email/request", response_model=MessageResponse)
async def request_email_verification(
    request: Request,
    body: VerificationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """(Re)send a verification email.

    Always returns the same generic message so the endpoint can't be used to
    probe which addresses have accounts. The email is only actually sent when a
    matching, not-yet-verified user exists.
    """
    _enforce_signup_rate_limit(request)
    user = await user_service.repository.get_by_email(db, body.email)
    if user is not None and not user.is_verified:
        background_tasks.add_task(email_service.send_verification_email, user)
    else:
        logger.info("Verification requested for non-eligible email=%s", body.email)
    return MessageResponse(
        detail="If an account exists for that address, a verification email has been sent."
    )


@router.post("/verify-email/confirm", response_model=MessageResponse)
async def confirm_email_verification(
    body: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Confirm an email address using the token from the verification link."""
    user = await email_service.verify_email_token(db, body.token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token.",
        )
    return MessageResponse(detail="Email verified successfully.")
