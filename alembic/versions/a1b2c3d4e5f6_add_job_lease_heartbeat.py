"""add job lease/heartbeat for stuck-PROCESSING recovery

Adds the lease columns that let a reaper reclaim jobs whose worker died mid-run
(RCA-1): ``heartbeat_at`` (liveness stamp refreshed while a worker processes the
job), ``attempts`` and ``max_attempts`` (bound how many times an abandoned job is
requeued before it is failed as a poison pill).

Also adds a partial index on ``heartbeat_at`` restricted to in-flight jobs so the
reaper's periodic sweep stays cheap as completed/failed rows accumulate. The
predicate matches the *stored* enum label, which is the uppercase member name
(``'PROCESSING'``), not the value.

Revision ID: a1b2c3d4e5f6
Revises: f2d4b6a8c0e1
Create Date: 2026-07-15 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f2d4b6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STALE_INDEX = "ix_processing_jobs_processing_heartbeat"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "processing_jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
    )
    # Cheap reaper scans: index only the in-flight rows it ever looks at.
    op.create_index(
        _STALE_INDEX,
        "processing_jobs",
        ["heartbeat_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(_STALE_INDEX, table_name="processing_jobs")
    op.drop_column("processing_jobs", "max_attempts")
    op.drop_column("processing_jobs", "attempts")
    op.drop_column("processing_jobs", "heartbeat_at")
