import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.subscription import Subscription, SubscriptionStatus
from app.repositories.base_repository import BaseRepository
from app.schemas.billing import SubscriptionCreate, SubscriptionUpdate


class SubscriptionRepository(
    BaseRepository[Subscription, SubscriptionCreate, SubscriptionUpdate]
):
    """Access to a user's subscription (with its plan eagerly loaded).

    Reads are keyed three ways: by user (app-facing), and by Stripe customer /
    subscription id (webhook-facing, since ``customer.subscription.*`` events
    carry no app user id).
    """

    async def get_for_user(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> Subscription | None:
        result = await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .options(selectinload(Subscription.plan))
        )
        return result.scalars().first()

    async def get_by_stripe_subscription_id(
        self, db: AsyncSession, stripe_subscription_id: str
    ) -> Subscription | None:
        result = await db.execute(
            select(Subscription)
            .where(Subscription.stripe_subscription_id == stripe_subscription_id)
            .options(selectinload(Subscription.plan))
        )
        return result.scalars().first()

    async def get_by_stripe_customer_id(
        self, db: AsyncSession, stripe_customer_id: str
    ) -> Subscription | None:
        result = await db.execute(
            select(Subscription)
            .where(Subscription.stripe_customer_id == stripe_customer_id)
            .options(selectinload(Subscription.plan))
        )
        return result.scalars().first()

    async def count_by_status(self, db: AsyncSession) -> dict[SubscriptionStatus, int]:
        """Subscription counts grouped by status. Absent statuses are missing; the
        service fills them with 0."""
        result = await db.execute(
            select(Subscription.status, func.count()).group_by(Subscription.status)
        )
        return {status: int(count) for status, count in result.all()}

    async def list_active_with_plan(self, db: AsyncSession) -> Sequence[Subscription]:
        """Every ACTIVE subscription with its ``plan`` eager-loaded.

        The revenue calculation reads ``plan.price_monthly_cents`` /
        ``price_annual_cents`` per row, so eager-loading avoids an N+1. Only ACTIVE
        subscriptions count toward MRR — trialing/past-due/cancelled don't bill.
        """
        result = await db.execute(
            select(Subscription)
            .where(Subscription.status == SubscriptionStatus.ACTIVE)
            .options(selectinload(Subscription.plan))
        )
        return result.scalars().all()


subscription_repository = SubscriptionRepository(Subscription)
