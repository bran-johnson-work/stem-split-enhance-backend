"""make users.password_hash nullable (OAuth2 support)

Allow passwordless accounts so future OAuth2 / social-login users can exist
without a local password (``password_hash = NULL``). Such accounts are barred
from password login at the application layer (``verify_password`` rejects a
null/empty hash).

Revision ID: f1a2b3c4d5e6
Revises: c3d9a1b47e02
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'c3d9a1b47e02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema.

    Not safe if any passwordless (OAuth) rows exist — they would violate the
    restored NOT NULL. Backfill or remove such rows before downgrading.
    """
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=False,
    )
