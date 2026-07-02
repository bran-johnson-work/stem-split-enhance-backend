from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
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
