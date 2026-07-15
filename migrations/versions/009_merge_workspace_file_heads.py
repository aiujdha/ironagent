"""Merge remaining workspace file migration heads.

Revision ID: 009_merge_workspace_file_heads
Revises: 003_workspace_file_object_storage, 003_workspace_file_storage_metadata,
    003_workspace_file_storage_sync, 004_workspace_file_storage_metadata_legacy,
    008_users_role_column
"""

from __future__ import annotations


revision = "009_merge_workspace_file_heads"
down_revision = (
    "003_workspace_file_object_storage",
    "003_workspace_file_storage_metadata",
    "003_workspace_file_storage_sync",
    "004_workspace_file_storage_metadata_legacy",
    "008_users_role_column",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
