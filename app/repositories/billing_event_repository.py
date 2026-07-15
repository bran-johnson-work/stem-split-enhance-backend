from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_event import BillingEvent
from app.repositories.base_repository import BaseRepository
from app.schemas.billing import BillingEventCreate


class BillingEventRepository(BaseRepository[BillingEvent, BillingEventCreate, BillingEventCreate]):
    """Idempotency log for Stripe webhooks (dedupe on ``stripe_event_id``)."""

    async def get_by_stripe_event_id(
        self, db: AsyncSession, stripe_event_id: str
    ) -> BillingEvent | None:
        result = await db.execute(
            select(BillingEvent).where(BillingEvent.stripe_event_id == stripe_event_id)
        )
        return result.scalars().first()


billing_event_repository = BillingEventRepository(BillingEvent)
