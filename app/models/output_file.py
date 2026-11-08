import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.processing_job import ProcessingJob
    from app.models.project import Project

# Every stem name the engine can produce, in display order: vocals, the drum kit (full +
# kick/snare/hi-hats), bass, the two instruments, the residual, and the untouched original.
#
# The API layer reads this to order the comparison view's stem list. It deliberately
# duplicates ``app.engine.pipeline.FULL_STEM_ORDER`` rather than importing it — the engine
# module pulls in torch, which the API image does not install. Keep the two in sync when
# adding a stem.
STEM_ORDER: tuple[str, ...] = (
    "vocals",
    "drums",
    "kick",
    "snare",
    "hihats",
    "bass",
    "guitar",
    "piano",
    "other",
    "original",
)

# All possible stem names produced by the engine (allow-list, not a required set — the
# content gate means a track yields only the stems it actually contains).
STEM_NAMES = frozenset(STEM_ORDER)


class OutputFile(Base, TimestampMixin):
    __tablename__ = "output_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
