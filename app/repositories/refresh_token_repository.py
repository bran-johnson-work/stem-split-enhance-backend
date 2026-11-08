import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken


class RefreshTokenRepository:
    """Data access for rotating refresh tokens."""

    async def get_by_hash(self, db: AsyncSession, token_hash: str) -> RefreshToken | None:
        """The row for a presented token, revoked or not.

        Revoked rows are returned deliberately: the caller needs to tell
        "unknown token" apart from "already-used token", because the second is a
        replay and triggers family revocation.
        """
        result = await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> RefreshToken:
        token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        db.add(token)
        await db.flush()
        await db.refresh(token)
        return token

    async def revoke(
        self,
        db: AsyncSession,
        token: RefreshToken,
        *,
        replaced_by_id: uuid.UUID | None = None,
    ) -> None:
        """Mark one token unusable, optionally recording its successor."""
        token.revoked_at = datetime.now(timezone.utc)
        if replaced_by_id is not None:
            token.replaced_by_id = replaced_by_id
        await db.flush()

    async def revoke_all_for_user(self, db: AsyncSession, user_id: uuid.UUID) -> int:
        """Revoke every live token for a user; returns how many were killed.

        Used by reuse detection (a replayed token means the family is
        compromised and we cannot tell the thief's device from the victim's) and
        available for a "sign out everywhere" action.
        """
        result = await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await db.flush()
        return int(result.rowcount or 0)

    async def delete_expired(self, db: AsyncSession, *, now: datetime | None = None) -> int:
        """Housekeeping: drop rows past expiry. Safe to run any time."""
        cutoff = now or datetime.now(timezone.utc)
        result = await db.execute(
            RefreshToken.__table__.delete().where(RefreshToken.expires_at < cutoff)
        )
        await db.flush()
        return int(result.rowcount or 0)


refresh_token_repository = RefreshTokenRepository()
