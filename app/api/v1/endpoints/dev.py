"""Local-only developer utilities — NEVER mounted outside ``APP_ENV=local``.

Registered conditionally in ``app/api/v1/router.py`` so these routes simply do
not exist in staging/production. They let you exercise billing flows in Swagger
without a Stripe account or the Stripe CLI, by writing the subscription state a
webhook would normally produce.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.subscription import SubscriptionStatus
from app.models.user import User
from app.repositories.plan_repository import plan_repository
from app.repositories.subscription_repository import subscription_repository
from app.schemas.billing import SubscriptionCreate, SubscriptionResponse
from app.services.billing_service import billing_service

router = APIRouter()


@router.post("/billing/simulate-subscription", response_model=SubscriptionResponse)
async def simulate_subscription(
    plan_name: str = Query(..., description="Plan name: free | creator | pro | studio"),
    status_value: SubscriptionStatus = Query(
        SubscriptionStatus.ACTIVE, description="Subscription status to simulate"
    ),
    cancel_at_period_end: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Force the current user's subscription to a given plan/status (no Stripe call).

    Mirrors what ``customer.subscription.*`` webhooks do, so you can watch
    ``GET /billing/subscription`` and the project quota change without any Stripe
    setup. Uses placeholder Stripe ids.
    """
    plan = await plan_repository.get_by_name(db, plan_name)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{plan_name}'.")

    now = datetime.now(timezone.utc)
    fields = {
        "plan_id": plan.id,
        "status": status_value,
        "current_period_start": now,
        "current_period_end": now + timedelta(days=30),
        "cancel_at_period_end": cancel_at_period_end,
        "cancelled_at": now if status_value is SubscriptionStatus.CANCELLED else None,
        "stripe_customer_id": f"cus_dev_{current_user.id.hex[:12]}",
        "stripe_subscription_id": f"sub_dev_{uuid.uuid4().hex[:12]}",
    }

    existing = await subscription_repository.get_for_user(db, current_user.id)
    if existing is not None:
        await subscription_repository.update(db, existing, fields)
    else:
        await subscription_repository.create(
            db, SubscriptionCreate(user_id=current_user.id, **fields)
        )
    await db.commit()
    return await billing_service.get_subscription_view(db, current_user.id)


@router.delete("/billing/subscription", status_code=status.HTTP_204_NO_CONTENT)
async def reset_subscription(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete the current user's subscription row, back to the free tier (reset)."""
    existing = await subscription_repository.get_for_user(db, current_user.id)
    if existing is not None:
        await db.delete(existing)
        await db.commit()
