"""Add Go Gateway HMAC nonce store.

Revision ID: 014_gateway_hmac_nonces
Revises: 013_runtime_proxy_targets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "014_gateway_hmac_nonces"
down_revision = "013_runtime_proxy_targets"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("gateway_hmac_nonces"):
        op.create_table(
            "gateway_hmac_nonces",
            sa.Column("client_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("nonce", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_gateway_hmac_nonces_expires_at",
            "gateway_hmac_nonces",
            ["expires_at"],
        )


def downgrade() -> None:
    if _has_table("gateway_hmac_nonces"):
        op.drop_index("ix_gateway_hmac_nonces_expires_at", table_name="gateway_hmac_nonces")
        op.drop_table("gateway_hmac_nonces")
