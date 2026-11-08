import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.billing_event import BillingEvent
    from app.models.project import Project
    from app.models.subscription import Subscription


class UserRole(str, enum.Enum):
    USER = "USER"  # owns/manages only their own account and resources
    ADMIN = "ADMIN"  # read-only visibility into all users/projects (internal utility)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    # Nullable so future OAuth2 / social-login accounts can exist without a
    # local password. Such accounts have ``password_hash = NULL`` and are barred
    # from password login by ``verify_password`` (a null/empty hash never
    # matches), so no submitted password — empty or otherwise — can log in.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.USER, nullable=False, index=True
    )

    subscription: Mapped["Subscription | None"] = relationship(
        "Subscription", back_populates="user", uselist=False
    )
    projects: Mapped[list["Project"]] = relationship(
        "Project", back_populates="user", cascade="all, delete-orphan"
    )
    billing_events: Mapped[list["BillingEvent"]] = relationship(
        "BillingEvent", back_populates="user"
    )
