import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.output_file import OutputFile
    from app.models.project import Project


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingJob(Base, TimestampMixin):
    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.PENDING, nullable=False, index=True
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current_step: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Worker lease / liveness (RCA-1: recover jobs stranded by a dead worker) ---
    # Refreshed by the owning worker while it processes the job. A PROCESSING job
    # whose heartbeat has gone stale is treated as abandoned and reclaimed by the
    # reaper (see app/worker/reaper.py). Set at claim time and on every heartbeat.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Incremented on each claim. The reaper requeues an abandoned job while
    # ``attempts < max_attempts`` and marks it FAILED once the cap is reached, so a
    # job that reliably kills its worker (a "poison pill") can't loop forever.
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=3, server_default="3", nullable=False
    )

    project: Mapped["Project"] = relationship("Project", back_populates="processing_jobs")
    output_files: Mapped[list["OutputFile"]] = relationship(
        "OutputFile", back_populates="job", cascade="all, delete-orphan"
    )
