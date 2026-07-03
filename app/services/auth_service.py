from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.revoked_token_repository import revoked_token_repository


class AuthService:
    """Business logic for authentication/session lifecycle."""

    def __init__(self) -> None:
        self.revoked_tokens = revoked_token_repository

    async def logout(self, db: AsyncSession, jti: str, exp_timestamp: int) -> None:
        """Revoke a token by its jti until its natural expiry."""
        expires_at = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
        await self.revoked_tokens.add(db, jti, expires_at)
        await db.commit()


auth_service = AuthService()
