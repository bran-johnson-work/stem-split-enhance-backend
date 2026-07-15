import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.models.project import ProjectStatus


class AdminProjectResponse(BaseModel):
    """Admin-facing view of a project: status plus output availability.

    Built explicitly by ``admin_service`` (not via ``from_attributes``) because
    ``owner_email`` and the output-availability fields are derived, not columns.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    owner_email: EmailStr
    name: str
    status: ProjectStatus
    output_file_count: int
    has_outputs: bool
    created_at: datetime
    updated_at: datetime
