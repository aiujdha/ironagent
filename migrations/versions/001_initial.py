"""Create assistant, approval, and audit storage tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistants",
        sa.Column("assistant_id", sa.String(length=191), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("capability_profile", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("iron_agent_config", sa.JSON(), nullable=False),
        sa.Column("workspace_config", sa.JSON(), nullable=False),
        sa.Column("runtime", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_assistants_name", "assistants", ["name"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=191), primary_key=True, nullable=False),
        sa.Column("tool_name", sa.String(length=191), nullable=False),
        sa.Column("assistant_id", sa.String(length=191), nullable=True),
        sa.Column("capability_profile", sa.String(length=64), nullable=True),
        sa.Column("approval_scope", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approvals_assistant_id", "approvals", ["assistant_id"], unique=False)
    op.create_index("ix_approvals_approval_scope", "approvals", ["approval_scope"], unique=False)
    op.create_index("ix_approvals_requested_at", "approvals", ["requested_at"], unique=False)
    op.create_index("ix_approvals_status", "approvals", ["status"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=191), nullable=False),
        sa.Column("hook_name", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.String(length=191), nullable=True),
        sa.Column("capability_profile", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=191), nullable=True),
        sa.Column("decision", sa.String(length=64), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_assistant_id", "audit_events", ["assistant_id"], unique=False)
    op.create_index("ix_audit_events_hook_name", "audit_events", ["hook_name"], unique=False)
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"], unique=False)
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"], unique=False)
    op.create_index(
        "ix_audit_events_request_hook",
        "audit_events",
        ["request_id", "hook_name"],
        unique=True,
    )
    op.create_index("ix_audit_events_tool_name", "audit_events", ["tool_name"], unique=False)


def downgrade() -> None:
    for index_name in reversed(_audit_index_names()):
        op.drop_index(index_name, table_name="audit_events")
    op.drop_table("audit_events")

    for index_name in reversed(_approval_index_names()):
        op.drop_index(index_name, table_name="approvals")
    op.drop_table("approvals")

    op.drop_index("ix_assistants_name", table_name="assistants")
    op.drop_table("assistants")


def _approval_index_names() -> Sequence[str]:
    return (
        "ix_approvals_status",
        "ix_approvals_requested_at",
        "ix_approvals_approval_scope",
        "ix_approvals_assistant_id",
    )


def _audit_index_names() -> Sequence[str]:
    return (
        "ix_audit_events_tool_name",
        "ix_audit_events_request_hook",
        "ix_audit_events_request_id",
        "ix_audit_events_occurred_at",
        "ix_audit_events_hook_name",
        "ix_audit_events_assistant_id",
    )
