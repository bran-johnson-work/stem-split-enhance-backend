"""add billing intervals to plans

Revision ID: c8a1d4e2b6f3
Revises: a3f7c1b9d2e5
Create Date: 2026-07-11 00:00:00.000000

Adds monthly+annual billing support to ``plans``:

- renames ``stripe_price_id`` -> ``stripe_price_id_monthly`` and adds
  ``stripe_price_id_annual`` (one Stripe Price per interval),
- adds ``price_annual_cents`` (whole-year price, backfilled to 0),
- adds a ``features`` JSON column for pricing-page bullets + non-enforced
  feature flags.

The actual Stripe price ids are environment-specific (test vs live) and are
populated out-of-band by ``scripts/set_stripe_price_ids.py``, not here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8a1d4e2b6f3"
down_revision: Union[str, Sequence[str], None] = "a3f7c1b9d2e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("plans", "stripe_price_id", new_column_name="stripe_price_id_monthly")
    op.add_column(
        "plans", sa.Column("stripe_price_id_annual", sa.String(length=255), nullable=True)
    )

    # server_default='0' backfills existing rows so the NOT NULL holds; then drop
    # it so the DB matches the ORM (default applied in Python).
    op.add_column(
        "plans",
        sa.Column("price_annual_cents", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("plans", "price_annual_cents", server_default=None)

    op.add_column("plans", sa.Column("features", pg.JSON(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("plans", "features")
    op.drop_column("plans", "price_annual_cents")
    op.drop_column("plans", "stripe_price_id_annual")
    op.alter_column("plans", "stripe_price_id_monthly", new_column_name="stripe_price_id")
