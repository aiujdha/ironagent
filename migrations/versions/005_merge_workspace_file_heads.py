"""Bridge legacy workspace-file migration history to the current Alembic chain.

Revision ID: 005_merge_workspace_file_heads
Revises: 004_workspace_files_object_store_fields
"""

revision = "005_merge_workspace_file_heads"
down_revision = "004_workspace_files_object_store_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy environments may already be stamped with this revision identifier.
    # Keep this migration as a no-op bridge so newer revisions can continue from it.
    return None


def downgrade() -> None:
    return None
