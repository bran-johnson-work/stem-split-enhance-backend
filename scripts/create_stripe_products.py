"""Create Stripe Products + Prices for the paid plans and wire the ids into the DB.

This is the "create the plan first" step that makes ``POST /billing/checkout``
work: it creates one Stripe Product per paid plan (creator/pro/studio), a monthly
and an annual recurring Price for each, and writes the resulting price ids onto
``plans.stripe_price_id_monthly`` / ``stripe_price_id_annual``.

Prerequisites: STRIPE_SECRET_KEY set and plans seeded (`alembic upgrade head`).
Run once:

    python -m scripts.create_stripe_products

Idempotent: it reconciles the DB against what's already in Stripe rather than
blindly creating. A plan with both price ids already stored is skipped; otherwise
it reuses an existing product (matched on ``metadata.plan_name``) and its
month/year prices, creating only what's genuinely missing. This is what stops a
fresh local DB from spawning a *third* copy of every product on each run. Creating
Products/Prices does not charge anyone. As a guard against pointing at the wrong
account, the script refuses a live key (sk_live_...) unless CONFIRM_LIVE=1 is set.
Amounts come from the DB (cents).
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


async def _existing_by_plan() -> dict[str, dict]:
    """Map ``plan_name`` -> {product_id, month: price_id, year: price_id} from Stripe.

    Reconciles against what already exists so re-runs reuse products/prices instead
    of duplicating them. When the account has stray duplicates, the first match
    wins (run scripts.cleanup_duplicate_products to prune the rest).
    """
    products = await stripe.list_products()
    prices = await stripe.list_prices()
    by_plan: dict[str, dict] = {}
    for prod in products:
        name = (prod.get("metadata") or {}).get("plan_name")
        if name in PAID_PLANS and name not in by_plan:
            by_plan[name] = {"product_id": prod["id"]}
    prod_to_plan = {v["product_id"]: k for k, v in by_plan.items()}
    for price in prices:
        plan_name = prod_to_plan.get(price.get("product"))
        interval = (price.get("recurring") or {}).get("interval")
        if plan_name and interval in ("month", "year"):
            by_plan[plan_name].setdefault(interval, price["id"])
    return by_plan


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

    existing = await _existing_by_plan()

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

            found = existing.get(plan.name, {})
            product_id = found.get("product_id")
            if product_id:
                print(f"[reuse] {plan.name}: existing product {product_id}")
            else:
                product = await stripe.create_product(
                    name=f"KlearNote {plan.display_name}",
                    metadata={"plan_name": plan.name},
                )
                product_id = product["id"]
                print(f"[new] {plan.name}: created product {product_id}")

            monthly_id = found.get("month")
            if not monthly_id:
                monthly_id = (await stripe.create_price(
                    product_id=product_id,
                    unit_amount=plan.price_monthly_cents,
                    interval="month",
                    metadata={"plan_name": plan.name, "interval": "monthly"},
                ))["id"]
            annual_id = found.get("year")
            if not annual_id:
                annual_id = (await stripe.create_price(
                    product_id=product_id,
                    unit_amount=plan.price_annual_cents,
                    interval="year",
                    metadata={"plan_name": plan.name, "interval": "annual"},
                ))["id"]

            plan.stripe_price_id_monthly = monthly_id
            plan.stripe_price_id_annual = annual_id
            db.add(plan)
            print(f"[ok] {plan.name}: monthly={monthly_id} annual={annual_id}")

        await db.commit()
    print("Done. Checkout will now resolve a price for these plans.")


if __name__ == "__main__":
    asyncio.run(main())
