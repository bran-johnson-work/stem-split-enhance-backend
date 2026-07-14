import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.project import ProjectStatus

# Matches the ``String(255)`` columns; bounding at the schema returns a clean 422
# instead of letting an over-long value reach the DB and surface as a 500.
_NAME_MAX = 255
_FILENAME_MAX = 255
_CONTENT_TYPE_MAX = 255


class ProjectCreate(BaseModel):
    user_id: uuid.UUID
    name: str = Field(max_length=_NAME_MAX)
    status: ProjectStatus = ProjectStatus.PENDING


class ProjectCreateRequest(BaseModel):
    """Client payload for creating an empty project (before any file is uploaded)."""

    name: str | None = Field(default=None, max_length=_NAME_MAX)


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

    filename: str = Field(min_length=1, max_length=_FILENAME_MAX)
    content_type: str = Field(min_length=1, max_length=_CONTENT_TYPE_MAX)
    name: str | None = Field(default=None, max_length=_NAME_MAX)
    # When set, add the file to an existing project instead of creating a new one.
    project_id: uuid.UUID | None = None
    # Optional declared byte size for a fast-fail plan check before the upload.
    # Advisory only (the client may under-declare); the real size is enforced on
    # the stored bytes at upload-complete.
    file_size_bytes: int | None = Field(default=None, ge=1)


class FileUploadUrlRequest(BaseModel):
    """Client's request for a presigned URL to add a file to an existing project."""

    filename: str = Field(min_length=1, max_length=_FILENAME_MAX)
    content_type: str = Field(min_length=1, max_length=_CONTENT_TYPE_MAX)
    # See ``UploadUrlRequest.file_size_bytes``: advisory fast-fail hint.
    file_size_bytes: int | None = Field(default=None, ge=1)


class UploadUrlResponse(BaseModel):
    """A one-time URL the client uploads the audio file directly to Supabase with."""

    project_id: uuid.UUID
    storage_key: str
    url: str
    token: str
