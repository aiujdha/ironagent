"""Merge runtime registry and workspace file migration heads."""

from collections.abc import Sequence

revision: str = "015_merge_runtime_and_workspace_heads"
down_revision: str | tuple[str, ...] | None = (
    "011_repair_workspace_file_checksum",
    "014_gateway_hmac_nonces",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
