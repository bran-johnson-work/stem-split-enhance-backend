"""create restoration_jobs table

Durable record of runs on the external V2/V3 restoration service. That service
keeps jobs in memory and loses them on restart, and knows nothing about users or
projects — without this table a browser would poll a job id that stops resolving,
and a finished result would be unattributable.

Revision ID: c5e8a2d1f704
Revises: b8c2e4f6a1d3
Create Date: 2026-08-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e8a2d1f704"
down_revision: Union[str, Sequence[str], None] = "b8c2e4f6a1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "restoration_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.Enum("V2", "V3", name="restorationstage"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("QUEUED", "RUNNING", "DONE", "FAILED", name="restorationstatus"),
            nullable=False,
        ),
        sa.Column("remote_job_id", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=1024), nullable=False),
        sa.Column("output_key", sa.String(length=1024), nullable=True),
        sa.Column("output_bucket", sa.String(length=255), nullable=True),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("quality", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: losing a V2 row must not delete the V3 result that
        # was derived from it. The link is provenance, not ownership.
        sa.ForeignKeyConstraint(["parent_id"], ["restoration_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_restoration_jobs_project_id"), "restoration_jobs", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_restoration_jobs_user_id"), "restoration_jobs", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_restoration_jobs_status"), "restoration_jobs", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_restoration_jobs_remote_job_id"),
        "restoration_jobs",
        ["remote_job_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_restoration_jobs_parent_id"), "restoration_jobs", ["parent_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_restoration_jobs_parent_id"), table_name="restoration_jobs")
    op.drop_index(op.f("ix_restoration_jobs_remote_job_id"), table_name="restoration_jobs")
    op.drop_index(op.f("ix_restoration_jobs_status"), table_name="restoration_jobs")
    op.drop_index(op.f("ix_restoration_jobs_user_id"), table_name="restoration_jobs")
    op.drop_index(op.f("ix_restoration_jobs_project_id"), table_name="restoration_jobs")
    op.drop_table("restoration_jobs")
    # The table's enum types are not dropped by drop_table and would collide with a
    # re-run of upgrade().
    sa.Enum(name="restorationstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="restorationstage").drop(op.get_bind(), checkfirst=True)
