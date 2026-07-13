"""Populate each plan's Stripe price ids from environment variables.

Stripe price ids are environment-specific (test vs live), so they are set here
rather than in a migration. After creating your Products/Prices in the Stripe
dashboard, export the ids and run this once (re-runnable / idempotent):

    export STRIPE_PRICE_CREATOR_MONTHLY=price_...
    export STRIPE_PRICE_CREATOR_ANNUAL=price_...
    export STRIPE_PRICE_PRO_MONTHLY=price_...
    export STRIPE_PRICE_PRO_ANNUAL=price_...
    export STRIPE_PRICE_STUDIO_MONTHLY=price_...
    export STRIPE_PRICE_STUDIO_ANNUAL=price_...
    python -m scripts.set_stripe_price_ids

Only the plans/intervals you provide are touched; the free plan needs no price.
Uses the app's async (pooled) engine, so run it against the same DB the API uses.
"""

import asyncio
import os

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.plan import Plan

# plan name -> (monthly env var, annual env var)
PRICE_ENV = {
    "creator": ("STRIPE_PRICE_CREATOR_MONTHLY", "STRIPE_PRICE_CREATOR_ANNUAL"),
    "pro": ("STRIPE_PRICE_PRO_MONTHLY", "STRIPE_PRICE_PRO_ANNUAL"),
    "studio": ("STRIPE_PRICE_STUDIO_MONTHLY", "STRIPE_PRICE_STUDIO_ANNUAL"),
}


async def main() -> None:
    updated = 0
    async with AsyncSessionLocal() as db:
        for name, (monthly_env, annual_env) in PRICE_ENV.items():
            monthly = os.getenv(monthly_env)
            annual = os.getenv(annual_env)
            if not monthly and not annual:
                continue

            plan = (
                await db.execute(select(Plan).where(Plan.name == name))
            ).scalars().first()
            if plan is None:
                print(f"[skip] plan '{name}' not found (run migrations first)")
                continue

            if monthly:
                plan.stripe_price_id_monthly = monthly
            if annual:
                plan.stripe_price_id_annual = annual
            db.add(plan)
            updated += 1
            print(f"[ok] {name}: monthly={monthly or '—'} annual={annual or '—'}")

        await db.commit()
    print(f"Done. Updated {updated} plan(s).")


if __name__ == "__main__":
    asyncio.run(main())
