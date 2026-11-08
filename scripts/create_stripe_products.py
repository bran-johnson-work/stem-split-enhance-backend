"""Create Stripe Products + Prices for the paid plans and wire the ids into the DB.

This is the "create the plan first" step that makes ``POST /billing/checkout``
work: it creates one Stripe Product per paid plan (creator/pro/studio), a monthly
and an annual recurring Price for each, and writes the resulting price ids onto
``plans.stripe_price_id_monthly`` / ``stripe_price_id_annual``.

Prerequisites: STRIPE_SECRET_KEY set and plans seeded (`alembic upgrade head`).
Run once:

    python -m scripts.create_stripe_products

Idempotent-ish: a plan that already has BOTH price ids is skipped (re-run after a
partial failure is safe). Creating Products/Prices does not charge anyone. As a
guard against pointing at the wrong account, the script refuses a live key
(sk_live_...) unless CONFIRM_LIVE=1 is set. Amounts come from the DB (cents).
"""

import asyncio
import os

from sqlalchemy import select

from app.core import stripe
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.plan import Plan

# Only paid tiers get Stripe prices; the free/Trial plan needs none.
PAID_PLANS = ["creator", "pro", "studio"]


async def main() -> None:
    settings = get_settings()
    if not settings.billing_enabled:
        print("[abort] STRIPE_SECRET_KEY is not set — nothing to do.")
        return

    mode = "LIVE" if settings.stripe_secret_key.startswith("sk_live_") else "test"
    if mode == "LIVE" and os.getenv("CONFIRM_LIVE") != "1":
        print("[abort] Refusing to create products with a LIVE key. Set CONFIRM_LIVE=1 to proceed.")
        return
    print(f"Creating Stripe products in {mode} mode...")

    async with AsyncSessionLocal() as db:
        plans = (
            await db.execute(select(Plan).where(Plan.name.in_(PAID_PLANS)))
        ).scalars().all()
        if not plans:
            print("[abort] No paid plans found. Run `alembic upgrade head` first.")
            return

        for plan in plans:
            if plan.stripe_price_id_monthly and plan.stripe_price_id_annual:
                print(
                    f"[skip] {plan.name}: already wired "
                    f"(monthly={plan.stripe_price_id_monthly}, "
                    f"annual={plan.stripe_price_id_annual})"
                )
                continue

            product = await stripe.create_product(
                name=f"KlearNote {plan.display_name}",
                metadata={"plan_name": plan.name},
            )
            monthly = await stripe.create_price(
                product_id=product["id"],
                unit_amount=plan.price_monthly_cents,
                interval="month",
                metadata={"plan_name": plan.name, "interval": "monthly"},
            )
            annual = await stripe.create_price(
                product_id=product["id"],
                unit_amount=plan.price_annual_cents,
                interval="year",
                metadata={"plan_name": plan.name, "interval": "annual"},
            )

            plan.stripe_price_id_monthly = monthly["id"]
            plan.stripe_price_id_annual = annual["id"]
            db.add(plan)
            print(f"[ok] {plan.name}: product={product['id']} "
                  f"monthly={monthly['id']} annual={annual['id']}")

        await db.commit()
    print("Done. Checkout will now resolve a price for these plans.")


if __name__ == "__main__":
    asyncio.run(main())
