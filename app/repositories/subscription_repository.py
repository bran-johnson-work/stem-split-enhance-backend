import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.subscription import Subscription
from app.repositories.base_repository import BaseRepository


class SubscriptionRepository(BaseRepository[Subscription, BaseModel, BaseModel]):
    """Read access for a user's subscription (with its plan eagerly loaded)."""

    async def get_for_user(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> Subscription | None:
        result = await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .options(selectinload(Subscription.plan))
        )
        return result.scalars().first()


subscription_repository = SubscriptionRepository(Subscription)
