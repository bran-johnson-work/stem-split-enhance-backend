from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import Plan
from app.repositories.base_repository import BaseRepository


class PlanRepository(BaseRepository[Plan, BaseModel, BaseModel]):
    """Read access for billing plans (no create/update path needed here)."""

    async def get_by_name(self, db: AsyncSession, name: str) -> Plan | None:
        result = await db.execute(select(Plan).where(Plan.name == name))
        return result.scalars().first()


plan_repository = PlanRepository(Plan)
