from sqlalchemy.ext.asyncio import AsyncSession
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import user_repository
from app.schemas.user import UserCreate, UserUpdate

class UserService:
    async def authenticate(self, db: AsyncSession, email: str, password: str) -> User | None:
        """Authenticate a user by email and password."""
        user = await user_repository.get_by_email(db, email=email)
        if not user:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    async def create_user(self, db: AsyncSession, user_in: UserCreate) -> User:
        """Create a new user after verifying that the email is unique."""
        existing_user = await user_repository.get_by_email(db, email=user_in.email)
        if existing_user:
            raise ValueError("Email already registered")
        
        # Hash the password and replace it in the data dictionary
        hashed = hash_password(user_in.password)
        db_obj = User(
            email=user_in.email,
            password_hash=hashed,
            full_name=user_in.full_name,
            is_active=True,
            is_verified=False
        )
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def update_user(self, db: AsyncSession, db_obj: User, user_in: UserUpdate) -> User:
        """Update a user's details, hashing the password if it is updated."""
        update_data = user_in.model_dump(exclude_unset=True)
        if "password" in update_data and update_data["password"]:
            update_data["password_hash"] = hash_password(update_data.pop("password"))
        
        for field, value in update_data.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

user_service = UserService()
