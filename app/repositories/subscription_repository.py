import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.subscription import Subscription
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


subscription_repository = SubscriptionRepository(Subscription)
