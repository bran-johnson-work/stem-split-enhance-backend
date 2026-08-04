"""add engine_version + engine_config to processing_jobs

Lets one Postgres queue feed two differently-built worker fleets: a v1 container
(HT-Demucs, as deployed today) and a v2 container (Mel-Band RoFormer). Each worker
claims only jobs stamped with its own engine version, so the two never steal each
other's work.

``engine_config`` records the settings the run actually used (models, shifts, overlap,
silence thresholds). Without it a v2 stem rendered at overlap 2 is indistinguishable
from one rendered at overlap 4 six months later, which makes A/B results unreadable.

Revision ID: b8c2e4f6a1d3
Revises: f3e1a2c4b5d6
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8c2e4f6a1d3"
down_revision = "f3e1a2c4b5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default 'v1' backfills every existing row, so jobs queued before this
    # migration stay claimable by the v1 fleet instead of becoming orphans.
    op.add_column(
        "processing_jobs",
        sa.Column(
            "engine_version",
            sa.String(length=16),
            nullable=False,
            server_default="v1",
        ),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("engine_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # The claim query is `WHERE status = 'pending' AND engine_version = ANY(...)
    # ORDER BY created_at`, so index all three in that order.
    op.create_index(
        "ix_processing_jobs_claim",
        "processing_jobs",
        ["status", "engine_version", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_claim", table_name="processing_jobs")
    op.drop_column("processing_jobs", "engine_config")
    op.drop_column("processing_jobs", "engine_version")
