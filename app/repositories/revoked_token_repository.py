from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.revoked_token import RevokedToken


class RevokedTokenRepository:
    """Data access for the JWT ``jti`` denylist."""

    async def is_revoked(self, db: AsyncSession, jti: str) -> bool:
        result = await db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti))
        return result.first() is not None

    async def add(self, db: AsyncSession, jti: str, expires_at: datetime) -> None:
        """Record a jti as revoked. No-op if it is already present."""
        if await self.is_revoked(db, jti):
            return
        db.add(RevokedToken(jti=jti, expires_at=expires_at))
        await db.flush()


revoked_token_repository = RevokedTokenRepository()
