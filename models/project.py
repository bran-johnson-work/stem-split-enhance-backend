import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from models.output_file import OutputFile
    from models.processing_job import ProcessingJob
    from models.project_file import ProjectFile
    from models.user import User


class ProjectStatus(str, enum.Enum):
    PENDING = "pending"        # created, no file yet
    UPLOADING = "uploading"    # file upload in progress
    QUEUED = "queued"          # file received, waiting for engine
    PROCESSING = "processing"  # engine is working
    COMPLETED = "completed"    # stems ready for download
    FAILED = "failed"          # unrecoverable error


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.PENDING, nullable=False, index=True
    )

    user: Mapped["User"] = relationship("User", back_populates="projects")
    input_file: Mapped["ProjectFile | None"] = relationship(
        "ProjectFile", back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    processing_jobs: Mapped[list["ProcessingJob"]] = relationship(
        "ProcessingJob", back_populates="project", cascade="all, delete-orphan",
        order_by="ProcessingJob.created_at"
    )
    output_files: Mapped[list["OutputFile"]] = relationship(
        "OutputFile", back_populates="project", cascade="all, delete-orphan"
    )
