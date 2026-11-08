"""add cancel_at_period_end to subscriptions

Revision ID: e1c3a5b7d9f2
Revises: d9b2e5f3c7a4
Create Date: 2026-07-12 00:00:00.000000

Adds a ``cancel_at_period_end`` flag so the UI can distinguish "active and
renewing" from "active but scheduled to cancel at period end". Backfilled to
false via a server_default, which is then dropped to match the ORM (default
applied in Python, like ``is_active``).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1c3a5b7d9f2"
down_revision: Union[str, Sequence[str], None] = "d9b2e5f3c7a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "subscriptions",
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("subscriptions", "cancel_at_period_end", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("subscriptions", "cancel_at_period_end")
