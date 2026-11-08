import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.restoration_job import RestorationJob, RestorationStatus


class RestorationJobRepository:
    """Data access for restoration jobs.

    Deliberately not a ``BaseRepository`` subclass: every row here is created from a
    live upstream response rather than a client-supplied ``*Create`` schema, so the
    generic CRUD signature would not fit without inventing a schema to satisfy it.
    """

    def __init__(self, model: type[RestorationJob] = RestorationJob) -> None:
        self.model = model

    async def create(self, db: AsyncSession, **values) -> RestorationJob:
        job = self.model(**values)
        db.add(job)
        await db.flush()
        await db.refresh(job)
        return job

    async def get_for_user(
        self, db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID
    ) -> RestorationJob | None:
        """Ownership is part of the lookup, so a caller cannot forget to check it."""
        result = await db.execute(
            select(self.model).where(self.model.id == job_id, self.model.user_id == user_id)
        )
        return result.scalars().first()

    async def list_for_project(
        self, db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> Sequence[RestorationJob]:
        result = await db.execute(
            select(self.model)
            .where(self.model.project_id == project_id, self.model.user_id == user_id)
            .order_by(self.model.created_at)
        )
        return result.scalars().all()

    async def get_active(
        self, db: AsyncSession, project_id: uuid.UUID, stage
    ) -> RestorationJob | None:
        """The in-flight job for this project and stage, if any.

        Used to make starting a run idempotent: the upstream queue is one job deep on
        one GPU, so a double-submitted stage would sit behind its own duplicate.
        """
        result = await db.execute(
            select(self.model)
            .where(
                self.model.project_id == project_id,
                self.model.stage == stage,
                self.model.status.in_((RestorationStatus.QUEUED, RestorationStatus.RUNNING)),
            )
            .order_by(self.model.created_at.desc())
        )
        return result.scalars().first()


restoration_job_repository = RestorationJobRepository(RestorationJob)
