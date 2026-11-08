from typing import Sequence

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import Plan
from app.repositories.base_repository import BaseRepository


class PlanRepository(BaseRepository[Plan, BaseModel, BaseModel]):
    """Read access for billing plans (no create/update path needed here)."""

    async def get_by_name(self, db: AsyncSession, name: str) -> Plan | None:
        result = await db.execute(select(Plan).where(Plan.name == name))
        return result.scalars().first()

    async def list_active(self, db: AsyncSession) -> Sequence[Plan]:
        """Active plans, cheapest first — the pricing page order."""
        result = await db.execute(
            select(Plan)
            .where(Plan.is_active.is_(True))
            .order_by(Plan.price_monthly_cents.asc())
        )
        return result.scalars().all()

    async def get_by_stripe_price_id(
        self, db: AsyncSession, price_id: str
    ) -> Plan | None:
        """Resolve a plan from either of its Stripe price ids (webhook mapping)."""
        result = await db.execute(
            select(Plan).where(
                or_(
                    Plan.stripe_price_id_monthly == price_id,
                    Plan.stripe_price_id_annual == price_id,
                )
            )
        )
        return result.scalars().first()


plan_repository = PlanRepository(Plan)
