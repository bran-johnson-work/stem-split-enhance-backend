"""add billing_interval to subscriptions

Revision ID: f3e1a2c4b5d6
Revises: e7b1d9c2a4f8
Create Date: 2026-07-26 00:00:00.000000

Persists which cadence a subscription was purchased on ("monthly"/"annual").
Stripe doesn't carry the interval on a subscription object after the fact and the
row never stored it, so the frontend couldn't render annual vs monthly correctly.
It is now written from the Stripe price's recurring interval on every subscription
sync (``billing_service._on_subscription_upsert``).

Nullable String(16): legacy rows predate it (NULL = unknown, frontend defaults to
monthly), and a plain string avoids introducing a DB enum type for two values.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3e1a2c4b5d6"
down_revision: Union[str, Sequence[str], None] = "e7b1d9c2a4f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("billing_interval", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "billing_interval")
