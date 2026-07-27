
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3e1a2c4b5d6"
down_revision: Union[str, Sequence[str], None] = "e7b1d9c2a4f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("billing_interval", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "billing_interval")
