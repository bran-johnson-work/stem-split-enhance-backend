import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.processing_job import ProcessingJob
    from app.models.project import Project

# All possible stem names produced by the engine
STEM_NAMES = frozenset({
    "vocals", "drums", "kick", "snare", "hihats",
    "bass", "guitar", "piano", "other", "original",
})


class OutputFile(Base, TimestampMixin):
    __tablename__ = "output_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    stem_name: Mapped[str] = mapped_column(String(50), nullable=False)  # vocals | drums | etc.
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)  # S3 object key
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_enhanced: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    project: Mapped["Project"] = relationship("Project", back_populates="output_files")
    job: Mapped["ProcessingJob"] = relationship("ProcessingJob", back_populates="output_files")
