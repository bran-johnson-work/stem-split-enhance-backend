"""project_files one-to-many + usage-count index

Relax project_files from one-to-one to one-to-many (drop the UNIQUE on
project_id, keep a plain index for FK lookups) and add a composite
(user_id, created_at) index on projects to back monthly-quota COUNT queries.

Revision ID: c3d9a1b47e02
Revises: e497cdc726dc
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d9a1b47e02'
down_revision: Union[str, Sequence[str], None] = 'e497cdc726dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres auto-generates this name for UniqueConstraint('project_id').
_UNIQUE_NAME = "project_files_project_id_key"


def upgrade() -> None:
    """Upgrade schema."""
    # Allow a project to own many input files.
    op.drop_constraint(_UNIQUE_NAME, "project_files", type_="unique")
    op.create_index(
        op.f("ix_project_files_project_id"), "project_files", ["project_id"], unique=False
    )
    # Back per-user monthly project counts (quota enforcement).
    op.create_index(
        "ix_projects_user_id_created_at", "projects", ["user_id", "created_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_projects_user_id_created_at", table_name="projects")
    op.drop_index(op.f("ix_project_files_project_id"), table_name="project_files")
    # Restoring the UNIQUE constraint requires project_id to be unique again.
    op.create_unique_constraint(_UNIQUE_NAME, "project_files", ["project_id"])
