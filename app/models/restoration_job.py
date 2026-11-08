import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.user import User


class RestorationStage(str, enum.Enum):
    """Which half of the restoration pipeline this row represents.

    They are separate rows rather than one because they are separate upstream jobs
    that queue independently: V3 takes V2's *output* as its input, so it cannot even
    be submitted until V2 has finished and its result key exists.
    """

    V2 = "v2"
    V3 = "v3"


class RestorationStatus(str, enum.Enum):
    """Mirrors the upstream API's vocabulary rather than translating it.

    Renaming these to match ``JobStatus`` (pending/processing/completed) would mean
    two names for one upstream value and a mapping to keep correct in both
    directions, for no gain — this table exists to record what the upstream said.
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


TERMINAL_STATUSES = {RestorationStatus.DONE, RestorationStatus.FAILED}


class RestorationJob(Base, TimestampMixin):
    """One V2 or V3 run on the external restoration service.

    The upstream keeps jobs in memory and loses them on restart ("Jobs do not
    survive a restart"), and knows nothing about users or projects. This table is
    what makes a run durable, ownable, and answerable after the fact: without it a
    restart would strand the browser polling a job id that no longer resolves.
    """

    __tablename__ = "restoration_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised from the project so ownership can be checked without a join, and
    # so the S3 key prefix can be built without loading the project.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    stage: Mapped[RestorationStage] = mapped_column(Enum(RestorationStage), nullable=False)
    status: Mapped[RestorationStatus] = mapped_column(
        Enum(RestorationStatus), default=RestorationStatus.QUEUED, nullable=False, index=True
    )

    # The upstream's own job id. Indexed because polling looks a row up by it.
    remote_job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # What went in and what came out, both as bucket keys. `source_key` is the
    # project's input for a V2 run and the *previous row's* `output_key` for a V3
    # run — that link is the chain.
    source_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    output_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Set on a V3 row to the V2 row it consumed. Null for a V2 row, or for a V3 run
    # started directly against the original audio (V3-only, no repair stage).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restoration_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The upstream's step log, most recent last. It is the only progress signal the
    # pipeline emits — there is no percentage.
    progress: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # What each stage decided (flagged region, risk before/after, cutoff shift...).
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The optional A/B measurement against the input. Null unless it was asked for.
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The options the run was submitted with, so a result stays interpretable once
    # the defaults have moved on.
    options: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="restoration_jobs")
    user: Mapped["User"] = relationship("User")

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_STATUSES
