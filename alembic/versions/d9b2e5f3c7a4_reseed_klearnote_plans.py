"""reseed klearnote plans

Revision ID: d9b2e5f3c7a4
Revises: c8a1d4e2b6f3
Create Date: 2026-07-11 00:10:00.000000

Replaces the placeholder free/starter/pro seed with the KlearNote catalogue:
Trial (free), Creator, Pro, Studio — each priced monthly + annually. The old
``starter`` plan is deactivated rather than deleted so any subscription that
already references it keeps its foreign key.

Upsert is keyed on the unique ``plans.name`` so this is safe to re-run and does
not disturb Stripe price ids populated separately. Prices are in cents; annual
prices are the whole-year charge. ``max_projects_per_month = NULL`` = unlimited.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9b2e5f3c7a4"
down_revision: Union[str, Sequence[str], None] = "c8a1d4e2b6f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PLANS = [
    {
        "name": "free",
        "display_name": "Trial",
        "price_monthly_cents": 0,
        "price_annual_cents": 0,
        "max_projects_per_month": 3,
        "max_file_size_mb": 20,
        "features": {
            "description": "Try KlearNote with a few tracks.",
            "bullets": ["3 uploads", "Standard stems", "MP3 downloads"],
            "stem_quality": "standard",
            "download_format": "mp3",
            "enhanced_master": False,
            "priority_processing": False,
            "team_seats": False,
            "api_access": False,
            "highlighted": False,
        },
    },
    {
        "name": "creator",
        "display_name": "Creator",
        "price_monthly_cents": 1200,
        "price_annual_cents": 12000,
        "max_projects_per_month": 50,
        "max_file_size_mb": 100,
        "features": {
            "description": "For hobbyists and growing creators.",
            "bullets": ["50 uploads / mo", "HQ stems", "WAV downloads", "Enhanced master"],
            "stem_quality": "hq",
            "download_format": "wav",
            "enhanced_master": True,
            "priority_processing": False,
            "team_seats": False,
            "api_access": False,
            "highlighted": False,
        },
    },
    {
        "name": "pro",
        "display_name": "Pro",
        "price_monthly_cents": 2900,
        "price_annual_cents": 29000,
        "max_projects_per_month": None,
        "max_file_size_mb": 300,
        "features": {
            "description": "For professionals who ship constantly.",
            "bullets": [
                "Unlimited uploads",
                "Lossless stems",
                "Priority processing",
                "Advanced mastering",
            ],
            "stem_quality": "lossless",
            "download_format": "wav",
            "enhanced_master": True,
            "priority_processing": True,
            "team_seats": False,
            "api_access": False,
            "highlighted": True,
        },
    },
    {
        "name": "studio",
        "display_name": "Studio",
        "price_monthly_cents": 7900,
        "price_annual_cents": 79000,
        "max_projects_per_month": None,
        "max_file_size_mb": 500,
        "features": {
            "description": "For teams and studios at scale.",
            "bullets": ["Everything in Pro", "Team seats", "API access", "Dedicated support"],
            "stem_quality": "lossless",
            "download_format": "wav",
            "enhanced_master": True,
            "priority_processing": True,
            "team_seats": True,
            "api_access": True,
            "highlighted": False,
        },
    },
]


def _plans_table() -> sa.Table:
    return sa.table(
        "plans",
        sa.column("id", pg.UUID(as_uuid=True)),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("price_monthly_cents", sa.Integer),
        sa.column("price_annual_cents", sa.Integer),
        sa.column("max_projects_per_month", sa.Integer),
        sa.column("max_file_size_mb", sa.Integer),
        sa.column("features", pg.JSON),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def upgrade() -> None:
    import uuid

    now = datetime.now(timezone.utc)
    table = _plans_table()

    for plan in PLANS:
        stmt = pg.insert(table).values(
            id=uuid.uuid4(),
            is_active=True,
            created_at=now,
            updated_at=now,
            **plan,
        )
        # Upsert on the unique name: refresh pricing/limits/features but never
        # touch stripe_price_id_* (set out-of-band) or the primary key.
        stmt = stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={
                "display_name": stmt.excluded.display_name,
                "price_monthly_cents": stmt.excluded.price_monthly_cents,
                "price_annual_cents": stmt.excluded.price_annual_cents,
                "max_projects_per_month": stmt.excluded.max_projects_per_month,
                "max_file_size_mb": stmt.excluded.max_file_size_mb,
                "features": stmt.excluded.features,
                "is_active": True,
                "updated_at": now,
            },
        )
        op.execute(stmt)

    # Retire the placeholder tier from the earlier seed (keep the row for FK safety).
    op.execute("UPDATE plans SET is_active = false WHERE name = 'starter'")


def downgrade() -> None:
    # Data migration: reverting advertised pricing/features has no meaningful
    # inverse. Re-activate the legacy starter tier and leave the rest in place.
    op.execute("UPDATE plans SET is_active = true WHERE name = 'starter'")
