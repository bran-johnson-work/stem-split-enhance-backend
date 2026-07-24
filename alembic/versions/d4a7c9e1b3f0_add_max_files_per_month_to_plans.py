"""add max_files_per_month to plans

Revision ID: d4a7c9e1b3f0
Revises: b7e4f2a9c1d8
Create Date: 2026-07-22 00:00:00.000000

The metered unit becomes *audio files per month* rather than projects per month:
a project may hold many input files (one-to-many), so the plan cap now limits the
files a user can upload in a billing period across all their projects. Projects
themselves are unlimited (``max_projects_per_month`` is retained for display but
no longer enforced).

Seeds the per-tier caps: Trial (free) 3, Creator 50, Pro/Studio unlimited (NULL).
Idempotent upsert keyed on ``plans.name``; safe to re-run.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a7c9e1b3f0"
down_revision: Union[str, Sequence[str], None] = "b7e4f2a9c1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# name -> monthly audio-file allowance (None = unlimited)
FILE_CAPS = {
    "free": 3,
    "creator": 50,
    "pro": None,
    "studio": None,
}


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("max_files_per_month", sa.Integer(), nullable=True),
    )
    # Seed the caps onto the existing rows. A row that doesn't exist yet is simply
    # not matched; the plan reseed migration owns row creation.
    for name, cap in FILE_CAPS.items():
        op.execute(
            sa.text("UPDATE plans SET max_files_per_month = :cap WHERE name = :name").bindparams(
                cap=cap, name=name
            )
        )


def downgrade() -> None:
    op.drop_column("plans", "max_files_per_month")
