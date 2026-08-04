import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.processing_job import JobStatus


class ProcessingJobCreate(BaseModel):
    """Internal DTO for creating a job row (used by the repository)."""

    project_id: uuid.UUID
    status: JobStatus = JobStatus.PENDING
    current_step: str = ""
    # Which engine build must run this job; only a worker whose
    # WORKER_ENGINE_VERSIONS includes it will claim it.
    engine_version: str = "v1"


class ProcessingJobUpdate(BaseModel):
    status: JobStatus | None = None
    progress: float | None = None
    current_step: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class OutputFileResponse(BaseModel):
    id: uuid.UUID
    stem_name: str
    is_enhanced: bool
    file_size_bytes: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: JobStatus
    progress: float
    current_step: str
    error_message: str | None = None
    attempts: int = 0
    engine_version: str = "v1"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StemDownload(BaseModel):
    """A download reference for one output stem.

    Normally a short-lived S3 signed URL (``expires_in`` seconds). When the
    worker saves stems to a local folder (``STEMS_LOCAL_DIR``), this is a ``file://``
    URI pointing at the file on disk and ``expires_in`` is 0.
    """

    stem_name: str
    is_enhanced: bool
    url: str
    expires_in: int


class JobStemsResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    stems: list[StemDownload] = []


class VersionStems(BaseModel):
    """One engine version's result for a project, for the side-by-side comparison view.

    ``stems`` is empty while the version is still rendering or if it failed — the UI shows
    a per-version state rather than blocking the finished version behind the slow one.
    """

    engine_version: str
    job_id: uuid.UUID
    status: JobStatus
    engine_config: dict | None = None
    stems: list[StemDownload] = []


class StemsCompareResponse(BaseModel):
    """Every engine version that has run for a project, newest job per version.

    ``stem_names`` is the **union** across versions, in catalogue order. The versions can
    legitimately disagree about which stems exist — they use different models, so the
    content gate reaches different verdicts — and the UI needs the union to render an
    explicit "not detected in this version" cell instead of a silently missing row.
    """

    project_id: uuid.UUID
    stem_names: list[str] = []
    versions: list[VersionStems] = []
