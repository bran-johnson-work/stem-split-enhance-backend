"""Diagnose upload size limits: what the presign policy will actually enforce.

The presigned upload POST caps a file at the caller's plan ``max_file_size_mb``
(``ProjectService._max_upload_bytes``); the ``MAX_UPLOAD_SIZE_BYTES`` setting is
only a *fallback* used when no plan can be resolved. So a "Pro" user stuck at the
fallback size almost always means one of:

  * the plans table isn't seeded (``alembic upgrade head`` never ran here), or
  * the subscription isn't ACTIVE/TRIALING, or isn't linked to a plan.

This script makes that visible. Run it against the same DB the API uses:

    # every plan's advertised size cap + the fallback:
    python -m scripts.check_plan_limits

    # plus one user's *effective* plan and the exact presign ceiling:
    python -m scripts.check_plan_limits --email someone@example.com

Uses the app's async engine, so it reads live data, no mocks.
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.repositories.plan_repository import plan_repository
from app.repositories.subscription_repository import subscription_repository
from app.repositories.user_repository import user_repository
from app.services.project_service import project_service


def _mb(num_bytes: int) -> str:
    return f"{num_bytes // (1024 * 1024)} MB"


async def _report_plans(db) -> None:
    plans = await plan_repository.list_active(db)
    if not plans:
        print(
            "[!] No active plans found. The plans table is not seeded — every "
            "upload will fall back to MAX_UPLOAD_SIZE_BYTES "
            f"({_mb(get_settings().max_upload_size_bytes)}) regardless of tier. "
            "Run: alembic upgrade head"
        )
        return
    print("Active plans (name — display — per-file cap — monthly files):")
    for plan in plans:
        files = "unlimited" if plan.max_files_per_month is None else plan.max_files_per_month
        print(
            f"  - {plan.name:<8} {plan.display_name:<10} "
            f"{plan.max_file_size_mb:>4} MB   files/mo={files}"
        )
    print(
        f"\nFallback (no plan resolved): "
        f"{_mb(get_settings().max_upload_size_bytes)} (MAX_UPLOAD_SIZE_BYTES)"
    )


async def _report_user(db, email: str) -> None:
    user = await user_repository.get_by_email(db, email)
    if user is None:
        print(f"\n[!] No user found for {email!r}.")
        return

    subscription = await subscription_repository.get_for_user(db, user.id)
    plan = await project_service._effective_plan(db, user.id)
    ceiling = project_service._max_upload_bytes(plan)

    print(f"\nUser {email} ({user.id}):")
    if subscription is None:
        print("  subscription: none (defaults to the 'free' plan)")
    else:
        linked = (
            subscription.plan.name
            if subscription.plan is not None
            else "— (no plan_id linked!)"
        )
        print(f"  subscription: status={subscription.status.value} plan={linked}")
    if plan is None:
        print("  effective plan: NONE — plan could not be resolved")
    else:
        print(f"  effective plan: {plan.name} ({plan.max_file_size_mb} MB per file)")
    print(f"  >> presign will cap uploads at: {_mb(ceiling)}")
    if plan is None:
        print(
            "  >> This is the FALLBACK, not a tier. Seed plans / activate the "
            "subscription so this user gets their real limit."
        )


async def main(email: str | None) -> None:
    async with AsyncSessionLocal() as db:
        await _report_plans(db)
        if email:
            await _report_user(db, email)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="also resolve this user's effective upload cap")
    args = parser.parse_args()
    asyncio.run(main(args.email))
