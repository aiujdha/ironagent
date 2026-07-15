"""Add assistant soft-delete marker."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016_assistant_soft_delete"
down_revision: str | tuple[str, ...] | None = "015_merge_runtime_and_workspace_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assistants", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("assistants", "deleted_at")
