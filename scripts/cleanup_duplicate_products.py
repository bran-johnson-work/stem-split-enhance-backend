"""Archive duplicate/orphan Stripe plan products, keeping only what the DB uses.

The seeding script used to create a fresh product set on every run against a blank
DB, so an account can accumulate several identical "KlearNote Creator/Pro/Studio"
products. This prunes them: it keeps the product that owns the price ids currently
stored on the ``plans`` rows and archives the rest (product + its prices).

!!! DANGER — SHARED ACCOUNTS !!!
This decides what to KEEP from the *local* DB's ``plans`` rows. If another
environment (staging/prod) points its own DB at the SAME Stripe account but stores
DIFFERENT price ids, this will archive the prices that other env still checks out
against and break its checkout. Only run it when the Stripe account is used by this
DB alone. When in doubt, don't — reactivate with scripts.restore_archived_products.

Safe by design:
  * Dry-run unless CONFIRM=1 — prints exactly what it would archive.
  * Archiving is reversible (sets ``active=false``; un-archive by setting it true).
  * Never archives a price that has a live subscription (active/trialing/past_due),
    nor its product — a strong signal the price is in use by *some* environment.
  * It never touches the products the DB references, and never cancels subscriptions.
  * Refuses a live key unless CONFIRM_LIVE=1.

    python -m scripts.cleanup_duplicate_products         # preview
    CONFIRM=1 python -m scripts.cleanup_duplicate_products   # apply
"""

import asyncio
import os

from sqlalchemy import select

from app.core import stripe
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.plan import Plan

PAID_PLANS = ["creator", "pro", "studio"]


async def main() -> None:
    settings = get_settings()
    if not settings.billing_enabled:
        print("[abort] STRIPE_SECRET_KEY is not set — nothing to do.")
        return
    if settings.stripe_secret_key.startswith("sk_live_") and os.getenv("CONFIRM_LIVE") != "1":
        print("[abort] Refusing to modify a LIVE account. Set CONFIRM_LIVE=1 to proceed.")
        return

    apply = os.getenv("CONFIRM") == "1"
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN (set CONFIRM=1 to archive)'}\n")

    # The price ids the app actually uses — their owning products are kept.
    async with AsyncSessionLocal() as db:
        plans = (
            await db.execute(select(Plan).where(Plan.name.in_(PAID_PLANS)))
        ).scalars().all()
        kept_price_ids = set()
        for p in plans:
            kept_price_ids.update(
                filter(None, (p.stripe_price_id_monthly, p.stripe_price_id_annual))
            )

    products = await stripe.list_products()
    prices = await stripe.list_prices()

    # Price ids that back a live subscription (any environment on this account).
    # Archiving one of these is almost always a mistake, so we refuse to.
    live_states = {"active", "trialing", "past_due", "unpaid"}
    subs = (await stripe._get(
        "subscriptions", {"limit": 100, "status": "all", "expand[]": "data.items"}
    )).get("data", [])
    in_use_price_ids: set[str] = set()
    for s in subs:
        if s.get("status") in live_states:
            for it in (s.get("items") or {}).get("data", []):
                pid = (it.get("price") or {}).get("id")
                if pid:
                    in_use_price_ids.add(pid)

    # Which product owns each kept price -> keep that product.
    price_to_product = {pr["id"]: pr.get("product") for pr in prices}
    keep_products = {price_to_product.get(pid) for pid in kept_price_ids} - {None}
    # Also keep any product that owns a price with a live subscription.
    keep_products |= {price_to_product.get(pid) for pid in in_use_price_ids} - {None}
    prices_by_product: dict[str, list[str]] = {}
    for pr in prices:
        prices_by_product.setdefault(pr.get("product"), []).append(pr["id"])

    archived_products = archived_prices = 0
    for prod in products:
        name = (prod.get("metadata") or {}).get("plan_name")
        if name not in PAID_PLANS:
            continue
        if prod["id"] in keep_products:
            reason = "referenced by DB or has a live subscription"
            print(f"[keep]  {name}: {prod['id']} ({reason})")
            continue

        product_prices = prices_by_product.get(prod["id"], [])
        # Belt-and-braces: skip if any of its prices is in use (product-level guard
        # above should already cover this, but never archive an in-use price).
        in_use = [pid for pid in product_prices if pid in in_use_price_ids]
        if in_use:
            print(f"[keep]  {name}: {prod['id']} (price in use: {', '.join(in_use)})")
            continue

        print(f"[prune] {name}: {prod['id']} + {len(product_prices)} price(s)")
        for price_id in product_prices:
            if apply:
                await stripe.archive_price(price_id=price_id)
            archived_prices += 1
        if apply:
            await stripe.archive_product(product_id=prod["id"])
        archived_products += 1

    verb = "Archived" if apply else "Would archive"
    print(f"\n{verb} {archived_products} product(s) and {archived_prices} price(s).")
    if not apply and archived_products:
        print("Re-run with CONFIRM=1 to apply.")


if __name__ == "__main__":
    asyncio.run(main())
