import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.project import ProjectStatus


class ProjectCreate(BaseModel):
    user_id: uuid.UUID
    name: str
    status: ProjectStatus = ProjectStatus.PENDING


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
    input_file: ProjectFileResponse | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InputUrlResponse(BaseModel):
    url: str
    expires_in: int
