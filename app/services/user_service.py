from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import user_repository
from app.schemas.user import UserCreate, UserUpdate


class UserService:
    """Business logic for users."""

    def __init__(self) -> None:
        self.repository = user_repository

    async def create_user(self, db: AsyncSession, user_in: UserCreate) -> User:
        """Create a new user with hashed password."""
        # Check if email is already registered
        existing = await self.repository.get_by_email(db, user_in.email)
        if existing:
            raise ValueError("Email already registered")

        # Hash password and create user
        password_hash = hash_password(user_in.password)
        user = await self.repository.create_user_with_hash(
            db, user_in=user_in, password_hash=password_hash
        )
        await db.commit()
        return user

    async def authenticate(self, db: AsyncSession, email: str, password: str) -> User | None:
        """Authenticate user by email and password."""
        user = await self.repository.get_by_email(db, email)
        if not user:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    async def update_user(self, db: AsyncSession, user_id: Any, user_in: UserUpdate) -> User | None:
        """Update a user (including optional password hashing)."""
        user = await self.repository.get(db, user_id)
        if not user:
            return None

        update_data = user_in.model_dump(exclude_unset=True)
        if "password" in update_data and update_data["password"]:
            password = update_data.pop("password")
            update_data["password_hash"] = hash_password(password)

        updated_user = await self.repository.update(db, user, update_data)
        await db.commit()
        return updated_user


user_service = UserService()
