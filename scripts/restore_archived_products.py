"""Reactivate archived plan products/prices — undo of cleanup_duplicate_products.

Sets ``active=true`` on every inactive product whose ``metadata.plan_name`` is one
of our paid plans, and on every inactive price under those products. Products are
reactivated before their prices (Stripe rejects activating a price on an inactive
product). Idempotent; safe to run repeatedly.

    python -m scripts.restore_archived_products
"""

import asyncio
import os

from app.core import stripe
from app.core.config import get_settings

PAID_PLANS = ["creator", "pro", "studio"]


async def main() -> None:
    settings = get_settings()
    if not settings.billing_enabled:
        print("[abort] STRIPE_SECRET_KEY is not set — nothing to do.")
        return
    if settings.stripe_secret_key.startswith("sk_live_") and os.getenv("CONFIRM_LIVE") != "1":
        print("[abort] Refusing to modify a LIVE account. Set CONFIRM_LIVE=1 to proceed.")
        return

    inactive_products = await stripe.list_products(active=False)
    product_ids = {
        p["id"]
        for p in inactive_products
        if (p.get("metadata") or {}).get("plan_name") in PAID_PLANS
    }

    # Reactivate products first (a price can't be reactivated on an inactive product).
    for pid in product_ids:
        await stripe._post(f"products/{pid}", {"active": True})
        print(f"[product] reactivated {pid}")

    # Then reactivate their prices.
    inactive_prices = await stripe.list_prices(active=False)
    restored_prices = 0
    for price in inactive_prices:
        if price.get("product") in product_ids:
            await stripe._post(f"prices/{price['id']}", {"active": True})
            print(f"[price]   reactivated {price['id']} (product {price.get('product')})")
            restored_prices += 1

    print(f"\nReactivated {len(product_ids)} product(s) and {restored_prices} price(s).")


if __name__ == "__main__":
    asyncio.run(main())
