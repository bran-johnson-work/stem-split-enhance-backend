import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.project import ProjectStatus


class ProjectCreate(BaseModel):
    user_id: uuid.UUID
    name: str
    status: ProjectStatus = ProjectStatus.PENDING


class ProjectCreateRequest(BaseModel):
    """Client payload for creating an empty project (before any file is uploaded)."""

    name: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    status: ProjectStatus | None = None


class ProjectFileResponse(BaseModel):
    id: uuid.UUID
    original_filename: str
    storage_key: str
    file_size_bytes: int
    content_type: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    status: ProjectStatus
    input_files: list[ProjectFileResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InputUrlResponse(BaseModel):
    url: str
    expires_in: int


class UploadUrlRequest(BaseModel):
    """Client's request for a presigned upload URL, before any bytes are sent."""

    filename: str
    content_type: str
    name: str | None = None
    # When set, add the file to an existing project instead of creating a new one.
    project_id: uuid.UUID | None = None


class FileUploadUrlRequest(BaseModel):
    """Client's request for a presigned URL to add a file to an existing project."""

    filename: str
    content_type: str


class UploadUrlResponse(BaseModel):
    """A one-time URL the client uploads the audio file directly to Supabase with."""

    project_id: uuid.UUID
    storage_key: str
    url: str
    token: str
