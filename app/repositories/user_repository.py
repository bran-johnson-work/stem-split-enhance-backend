from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.repositories.base_repository import BaseRepository
from app.schemas.user import UserCreate, UserUpdate


class UserRepository(BaseRepository[User, UserCreate, UserUpdate]):
    """User-specific repository."""

    async def get_by_email(self, db: AsyncSession, email: str) -> User | None:
        """Get user by email."""
        result = await db.execute(select(User).where(User.email == email))
        return result.scalars().first()

    async def is_active(self, db: AsyncSession, user_id: Any) -> bool:
        """Check if user is active."""
        user = await self.get(db, user_id)
        return user.is_active if user else False

    async def count_all(self, db: AsyncSession) -> int:
        """Total number of users."""
        result = await db.execute(select(func.count()).select_from(User))
        return int(result.scalar_one() or 0)

    async def count_active(self, db: AsyncSession) -> int:
        """Users whose account is active."""
        result = await db.execute(
            select(func.count()).select_from(User).where(User.is_active.is_(True))
        )
        return int(result.scalar_one() or 0)

    async def count_verified(self, db: AsyncSession) -> int:
        """Users who have confirmed their email."""
        result = await db.execute(
            select(func.count()).select_from(User).where(User.is_verified.is_(True))
        )
        return int(result.scalar_one() or 0)

    async def count_admins(self, db: AsyncSession) -> int:
        """Users with the ADMIN role."""
        result = await db.execute(
            select(func.count()).select_from(User).where(User.role == UserRole.ADMIN)
        )
        return int(result.scalar_one() or 0)

    async def signups_by_day(
        self, db: AsyncSession, since: datetime
    ) -> list[tuple[datetime, int]]:
        """Count signups per UTC day at/after ``since``, oldest bucket first.

        Returns ``(day, count)`` pairs where ``day`` is the ``date_trunc('day')``
        of ``created_at``. Only non-empty days are returned; the service fills the
        gaps so the trend has one point per day.
        """
        day = func.date_trunc("day", User.created_at)
        result = await db.execute(
            select(day, func.count())
            .where(User.created_at >= since)
            .group_by(day)
            .order_by(day)
        )
        return [(row[0], int(row[1])) for row in result.all()]

    async def create_user_with_hash(
        self, db: AsyncSession, user_in: UserCreate, password_hash: str
    ) -> User:
        """Create a user with hashed password."""
        db_obj = User(
            email=user_in.email,
            password_hash=password_hash,
            full_name=user_in.full_name,
            is_active=True,
            is_verified=False,
        )
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj


user_repository = UserRepository(User)
