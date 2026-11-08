import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.processing_job import JobStatus


class ProcessingJobCreate(BaseModel):
    """Internal DTO for creating a job row (used by the repository)."""

    project_id: uuid.UUID
    status: JobStatus = JobStatus.PENDING
    current_step: str = ""


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
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StemDownload(BaseModel):
    """A signed URL for one output stem."""

    stem_name: str
    is_enhanced: bool
    url: str
    expires_in: int


class JobStemsResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    stems: list[StemDownload] = []
