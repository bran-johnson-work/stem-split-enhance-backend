"""add role to users

Revision ID: a3f7c1b9d2e5
Revises: f1a2b3c4d5e6
Create Date: 2026-07-09 00:00:00.000000

Adds a two-role ACL (USER / ADMIN) to the users table. Existing rows are
backfilled to USER: the column is added with a ``server_default`` of 'USER' so
every pre-existing row gets a valid role, then the default is dropped to match
the ORM model (which sets the default in Python, like ``is_active``). The
explicit UPDATE is a defensive, idempotent backfill — safe to re-run.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f7c1b9d2e5'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    userrole = sa.Enum('USER', 'ADMIN', name='userrole')
    userrole.create(op.get_bind(), checkfirst=True)

    # server_default='USER' backfills every existing row at add time so the
    # NOT NULL constraint holds without a separate two-step migration.
    op.add_column(
        'users',
        sa.Column('role', userrole, nullable=False, server_default='USER'),
    )

    # Defensive, idempotent backfill: guarantees no legacy row is left without a
    # role even if one somehow predates the server_default. Safe to re-run.
    op.execute("UPDATE users SET role = 'USER' WHERE role IS NULL")

    # Drop the server default so the DB schema matches the ORM model, where the
    # default is applied in Python (consistent with is_active / is_verified).
    op.alter_column('users', 'role', server_default=None)

    op.create_index(op.f('ix_users_role'), 'users', ['role'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_users_role'), table_name='users')
    op.drop_column('users', 'role')
    sa.Enum(name='userrole').drop(op.get_bind(), checkfirst=True)
