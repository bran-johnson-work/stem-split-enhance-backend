import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(Base, TimestampMixin):
    """A long-lived, rotating credential used to mint new access tokens.

    Deliberately **opaque** rather than a JWT: the point of a refresh token is
    that it can be revoked, and a self-contained JWT cannot be. The database row
    is the authority, so revoking is a single UPDATE.

    Only the SHA-256 hash of the token is stored. A leaked database dump
    therefore yields nothing usable, exactly as with ``users.password_hash``.

    Rotation: each refresh swaps the presented token for a new one and links the
    old row to its successor via ``replaced_by_id``. That chain is what makes
    theft detectable — see ``auth_service.rotate_refresh_token``.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 hex of the opaque token. Unique so a presented token maps to at
    # most one row, and indexed because every refresh looks up by it.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set when the token is rotated away, revoked on logout, or killed by reuse
    # detection. A NULL here is what makes a token usable.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The token this one was rotated into. Present only on rotated rows, and the
    # link that lets a whole family be revoked when a stolen token is replayed.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped["User"] = relationship("User", back_populates="refresh_tokens")
