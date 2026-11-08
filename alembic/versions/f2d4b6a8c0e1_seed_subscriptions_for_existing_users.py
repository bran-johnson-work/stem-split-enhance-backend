"""seed subscriptions for existing users

Revision ID: f2d4b6a8c0e1
Revises: e1c3a5b7d9f2
Create Date: 2026-07-12 00:20:00.000000

Backfills a subscription row for every existing user that lacks one:
- ADMIN users -> the ``studio`` plan,
- everyone else -> the ``free`` (Trial) plan.

Rows are ACTIVE with no Stripe ids (they were not created through checkout).
New users created after this migration are NOT seeded — the API reports them as
being on the free plan by default (``GET /billing/subscription`` ->
``has_subscription=false``). Idempotent: only users without a subscription and
whose target plan exists are inserted. Enum values are the uppercase member
names, matching how SQLAlchemy stores them (``subscriptionstatus``).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2d4b6a8c0e1"
down_revision: Union[str, Sequence[str], None] = "e1c3a5b7d9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO subscriptions (
            id, user_id, plan_id, status, cancel_at_period_end, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            u.id,
            (SELECT p.id FROM plans p
                WHERE p.name = CASE WHEN u.role = 'ADMIN' THEN 'studio' ELSE 'free' END),
            'ACTIVE'::subscriptionstatus,
            false,
            now(),
            now()
        FROM users u
        WHERE NOT EXISTS (
            SELECT 1 FROM subscriptions s WHERE s.user_id = u.id
        )
        AND EXISTS (
            SELECT 1 FROM plans p2
                WHERE p2.name = CASE WHEN u.role = 'ADMIN' THEN 'studio' ELSE 'free' END
        )
        """
    )


def downgrade() -> None:
    # Remove only the rows this migration created: ACTIVE, never linked to Stripe
    # (checkout- and dev-simulated rows always carry a stripe id).
    op.execute(
        """
        DELETE FROM subscriptions
        WHERE stripe_subscription_id IS NULL
          AND stripe_customer_id IS NULL
          AND status = 'ACTIVE'::subscriptionstatus
        """
    )
