"""seed default plans

Revision ID: d7fe8654843e
Revises: b4262afd44fc
Create Date: 2026-07-01 15:01:28.766630

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7fe8654843e'
down_revision: Union[str, Sequence[str], None] = 'b4262afd44fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    plans = sa.table(
        "plans",
        sa.column("id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("price_monthly_cents", sa.Integer),
        sa.column("max_projects_per_month", sa.Integer),
        sa.column("max_file_size_mb", sa.Integer),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(plans, [
        {"id": uuid.uuid4(), "name": "free", "display_name": "Free", "price_monthly_cents": 0,
         "max_projects_per_month": 3, "max_file_size_mb": 30, "is_active": True,
         "created_at": now, "updated_at": now},
        {"id": uuid.uuid4(), "name": "starter", "display_name": "Starter", "price_monthly_cents": 999,
         "max_projects_per_month": 20, "max_file_size_mb": 100, "is_active": True,
         "created_at": now, "updated_at": now},
        {"id": uuid.uuid4(), "name": "pro", "display_name": "Pro", "price_monthly_cents": 2999,
         "max_projects_per_month": None, "max_file_size_mb": 300, "is_active": True,
         "created_at": now, "updated_at": now},
    ])


def downgrade() -> None:
    op.execute("DELETE FROM plans WHERE name IN ('free', 'starter', 'pro')")
