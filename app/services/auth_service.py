import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_refresh_token, hash_refresh_token
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.refresh_token_repository import refresh_token_repository
from app.repositories.revoked_token_repository import revoked_token_repository
from app.repositories.user_repository import user_repository

logger = logging.getLogger(__name__)
settings = get_settings()


class RefreshError(Exception):
    """A refresh token could not be exchanged. Always surfaced as a 401.

    Deliberately carries no detail about *why*: "expired", "unknown" and
    "already used" are the same answer to a client, and distinguishing them
    would tell an attacker whether a guessed token ever existed.
    """


class AuthService:
    """Business logic for authentication/session lifecycle."""

    def __init__(self) -> None:
        self.revoked_tokens = revoked_token_repository
        self.refresh_tokens = refresh_token_repository
        self.users = user_repository

    async def logout(self, db: AsyncSession, jti: str, exp_timestamp: int) -> None:
        """Revoke an access token by its jti until its natural expiry."""
        expires_at = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
        await self.revoked_tokens.add(db, jti, expires_at)
        await db.commit()

    # -- Refresh tokens ----------------------------------------------------

    async def issue_refresh_token(self, db: AsyncSession, user_id: uuid.UUID) -> str:
        """Mint a refresh token for a freshly authenticated user.

        Does not commit — the caller owns the transaction, so login persists the
        token in the same unit of work as the rest of its bookkeeping.
        """
        token = create_refresh_token()
        await self.refresh_tokens.create(
            db,
            user_id=user_id,
            token_hash=hash_refresh_token(token),
            expires_at=self._expiry(),
        )
        return token

    async def rotate_refresh_token(self, db: AsyncSession, token: str) -> tuple[User, str]:
        """Exchange a refresh token for a new one, returning (user, new_token).

        Rotation is single-use: the presented token is revoked as part of the
        exchange. That is what makes theft *detectable* — a stolen token can be
        used at most once before the legitimate client's next refresh presents an
        already-revoked token (or vice versa).

        On such a replay every live token for that user is revoked. That logs
        every device out, which is the point: we cannot tell the thief's session
        from the victim's, so the safe move is to end all of them.

        Raises ``RefreshError`` for anything unusable.
        """
        record = await self.refresh_tokens.get_by_hash(db, hash_refresh_token(token))

        if record is None:
            raise RefreshError("Unknown refresh token")

        if record.revoked_at is not None:
            # A revoked token being presented is either a replay of a stolen
            # token or a client that lost the response to its last rotation.
            # Both are treated as compromise — the safe reading.
            revoked = await self.refresh_tokens.revoke_all_for_user(db, record.user_id)
            await db.commit()
            logger.warning(
                "Refresh token reuse detected user_id=%s; revoked %d live token(s)",
                record.user_id,
                revoked,
            )
            raise RefreshError("Refresh token already used")

        if self._is_expired(record):
            raise RefreshError("Refresh token expired")

        user = await self.users.get(db, record.user_id)
        if user is None or not user.is_active:
            # Deleted or deactivated after the token was issued.
            await self.refresh_tokens.revoke(db, record)
            await db.commit()
            raise RefreshError("Account is no longer active")

        new_token = create_refresh_token()
        successor = await self.refresh_tokens.create(
            db,
            user_id=user.id,
            token_hash=hash_refresh_token(new_token),
            expires_at=self._expiry(),
        )
        await self.refresh_tokens.revoke(db, record, replaced_by_id=successor.id)
        await db.commit()

        return user, new_token

    async def revoke_refresh_token(self, db: AsyncSession, token: str) -> None:
        """Retire one refresh token on logout. Unknown tokens are ignored.

        Logout must not fail because the client sent a stale token — the user's
        intent (end this session) is satisfied either way.
        """
        record = await self.refresh_tokens.get_by_hash(db, hash_refresh_token(token))
        if record is None or record.revoked_at is not None:
            return
        await self.refresh_tokens.revoke(db, record)
        await db.commit()

    async def revoke_all_refresh_tokens(self, db: AsyncSession, user_id: uuid.UUID) -> int:
        """End every session for a user (password change, "sign out everywhere")."""
        count = await self.refresh_tokens.revoke_all_for_user(db, user_id)
        await db.commit()
        return count

    @staticmethod
    def _expiry() -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)

    @staticmethod
    def _is_expired(record: RefreshToken) -> bool:
        expires_at = record.expires_at
        # Postgres hands back tz-aware datetimes, but a naive one (e.g. SQLite in
        # a test) would raise on comparison — normalise to UTC first.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)


auth_service = AuthService()
