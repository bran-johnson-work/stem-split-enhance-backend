import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, new_uuid


class RevokedToken(Base, TimestampMixin):
    """A denylist of consumed/invalidated JWT ``jti`` values.

    Populated on logout (access tokens) and when an email-verification token is
    used (enforcing single-use). Rows can be purged once ``expires_at`` has
    passed, since an expired token is already rejected by signature/exp validation.
    """

    __tablename__ = "revoked_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
