"""Re-pull every local subscription from Stripe and re-run the sync.

Use after a sync-logic fix (e.g. the item-level billing-period change) to repair
rows that were written by the old code — it fetches each subscription fresh from
Stripe and feeds it back through ``BillingService._on_subscription_upsert``, the
same path webhooks use, so ``current_period_end`` / interval / status are refilled.

    python -m scripts.resync_subscriptions

Read-only against Stripe; writes only the local DB. Skips rows with no
``stripe_subscription_id`` (never checked out).
"""

import asyncio

from sqlalchemy import select

from app.core import stripe
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.subscription import Subscription
from app.services.billing_service import billing_service


async def main() -> None:
    if not get_settings().billing_enabled:
        print("[abort] STRIPE_SECRET_KEY is not set — nothing to do.")
        return

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Subscription))).scalars().all()
        synced = 0
        for row in rows:
            if not row.stripe_subscription_id:
                print(f"[skip] {row.id}: no stripe_subscription_id")
                continue
            obj = await stripe.retrieve_subscription(
                subscription_id=row.stripe_subscription_id
            )
            await billing_service._on_subscription_upsert(db, obj)
            synced += 1
            print(f"[ok] {row.stripe_subscription_id}: re-synced")
        await db.commit()
    print(f"Done. Re-synced {synced} subscription(s).")


if __name__ == "__main__":
    asyncio.run(main())
