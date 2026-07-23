"""add avatar_key to users

Revision ID: e7b1d9c2a4f8
Revises: d4a7c9e1b3f0
Create Date: 2026-07-22 00:30:00.000000

Adds the nullable ``users.avatar_key`` column: the S3 object key of a user's
profile avatar (in the avatars bucket). Stored as a key, not a URL, so the API
serves a fresh short-lived signed GET URL per response.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b1d9c2a4f8"
down_revision: Union[str, Sequence[str], None] = "d4a7c9e1b3f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_key", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_key")
