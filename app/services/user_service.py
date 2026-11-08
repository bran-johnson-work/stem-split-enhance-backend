import logging
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import user_repository
from app.schemas.user import UserCreate, UserResponse, UserUpdate

logger = logging.getLogger(__name__)


class UserService:
    """Business logic for users."""

    def __init__(self) -> None:
        self.repository = user_repository

    # --- Avatars ---------------------------------------------------------

    async def avatar_url_for(self, user: User) -> str | None:
        """A short-lived signed GET URL for the user's avatar, or None.

        The avatar object is private (like audio), so the URL is regenerated per
        response from ``avatar_key`` rather than stored — it expires after
        ``avatar_url_expire_seconds``.
        """
        if not user.avatar_key:
            return None
        settings = get_settings()
        return await run_in_threadpool(
            storage.get_signed_url,
            storage.BUCKET_AVATARS,
            user.avatar_key,
            settings.avatar_url_expire_seconds,
        )

    async def serialize(self, user: User) -> UserResponse:
        """Build the API response for a user, injecting the signed avatar URL."""
        avatar_url = await self.avatar_url_for(user)
        return UserResponse.model_validate(user).model_copy(
            update={"avatar_url": avatar_url}
        )

    def _avatar_prefix(self, user: User) -> str:
        """Storage prefix for a user's avatar. The ``avatar/`` top-level folder
        keeps avatars cleanly grouped (and separated from audio, keyed
        ``{user_id}/{project}/…``, when they share a bucket)."""
        return f"avatar/{user.id}"

    def _avatar_key(self, user: User) -> str:
        """Deterministic storage key — one avatar object per user, overwritten on
        replace (its stored Content-Type is what the browser renders, so the lack
        of a file extension is fine)."""
        return f"{self._avatar_prefix(user)}/avatar"

    async def create_avatar_upload_url(
        self, *, user: User, content_type: str
    ) -> dict[str, Any]:
        """Mint a presigned POST to upload a new avatar image directly to S3.

        Rejects a non-image content type. The policy caps the upload at
        ``max_avatar_size_bytes`` and pins the exact Content-Type, so S3 refuses a
        too-large or wrong-typed file itself. Raises ``ValueError`` on a bad type.
        """
        settings = get_settings()
        content_type = (content_type or "").lower()
        if content_type not in settings.allowed_image_content_type_set:
            raise ValueError(f"Unsupported image content type: {content_type or 'unknown'}")

        key = self._avatar_key(user)
        signed = await run_in_threadpool(
            storage.create_signed_upload_url,
            storage.BUCKET_AVATARS,
            key,
            content_type=content_type,
            max_size_bytes=settings.max_avatar_size_bytes,
            expires_in=settings.s3_upload_url_expire_seconds,
        )
        return {"storage_key": key, **signed}

    async def complete_avatar_upload(self, db: AsyncSession, *, user: User) -> User:
        """Confirm the avatar upload landed and record it on the user.

        Verifies the object actually exists in storage (never trusting the client)
        before setting ``avatar_key``. Raises ``ValueError`` if nothing was
        uploaded.
        """
        key = self._avatar_key(user)
        objects = await run_in_threadpool(
            storage.list_objects, storage.BUCKET_AVATARS, self._avatar_prefix(user)
        )
        if not any(o.get("name") == "avatar" for o in objects):
            raise ValueError("No uploaded avatar found")

        updated = await self.repository.update(db, user, {"avatar_key": key})
        await db.commit()
        return updated

    async def remove_avatar(self, db: AsyncSession, *, user: User) -> User:
        """Delete the user's avatar (storage object best-effort) and clear the key."""
        if user.avatar_key:
            try:
                await run_in_threadpool(
                    storage.delete_file, storage.BUCKET_AVATARS, user.avatar_key
                )
            except Exception:  # pragma: no cover - storage cleanup is best-effort
                logger.warning("Failed to delete avatar object %s", user.avatar_key)
        updated = await self.repository.update(db, user, {"avatar_key": None})
        await db.commit()
        return updated

    async def create_user(self, db: AsyncSession, user_in: UserCreate) -> User:
        """Create a new user with hashed password."""
        # Check if email is already registered
        existing = await self.repository.get_by_email(db, user_in.email)
        if existing:
            raise ValueError("Email already registered")

        # Hash password and create user. The pre-check above is best-effort; the
        # DB unique constraint on ``email`` is the source of truth and closes the
        # check-then-insert race between concurrent registrations.
        password_hash = hash_password(user_in.password)
        try:
            user = await self.repository.create_user_with_hash(
                db, user_in=user_in, password_hash=password_hash
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise ValueError("Email already registered")
        return user

    async def authenticate(self, db: AsyncSession, email: str, password: str) -> User | None:
        """Authenticate user by email and password.

        Returns ``None`` for an unknown user, a wrong password, or a passwordless
        (OAuth2) account — the last is enforced by ``verify_password`` rejecting a
        null/empty stored hash.
        """
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

        # Enforce email uniqueness on change (the repository/DB has the unique
        # constraint, but check here to return a clean error instead of a 500).
        new_email = update_data.get("email")
        if new_email and new_email != user.email:
            existing = await self.repository.get_by_email(db, new_email)
            if existing is not None and existing.id != user.id:
                raise ValueError("Email already registered")

        if "password" in update_data and update_data["password"]:
            password = update_data.pop("password")
            update_data["password_hash"] = hash_password(password)

        try:
            updated_user = await self.repository.update(db, user, update_data)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise ValueError("Email already registered")
        return updated_user


user_service = UserService()
