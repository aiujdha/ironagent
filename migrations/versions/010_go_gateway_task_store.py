"""Add Go Gateway task store tables.

Revision ID: 010_go_gateway_task_store
Revises: 009_merge_workspace_file_heads
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010_go_gateway_task_store"
down_revision = "009_merge_workspace_file_heads"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("gateway_tasks"):
        op.create_table(
            "gateway_tasks",
            sa.Column("task_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("client_id", sa.String(length=191), nullable=False),
            sa.Column("request_id", sa.String(length=191), nullable=True),
            sa.Column("conversation_id", sa.String(length=191), nullable=True),
            sa.Column("user_id", sa.String(length=191), nullable=True),
            sa.Column("employee_no", sa.String(length=191), nullable=True),
            sa.Column("source_assistant_id", sa.String(length=191), nullable=True),
            sa.Column("target_assistant_id", sa.String(length=191), nullable=True),
            sa.Column("southbound_task_id", sa.String(length=191), nullable=True),
            sa.Column("approval_id", sa.String(length=191), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("stage", sa.String(length=64), nullable=True),
            sa.Column("fallback_query_url", sa.String(length=512), nullable=True),
            sa.Column("callback_url", sa.String(length=1024), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("failed_reason_code", sa.String(length=191), nullable=True),
            sa.Column("failed_reason_text", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("task_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_gateway_tasks_client_id", "gateway_tasks", ["client_id"])
        op.create_index("ix_gateway_tasks_status", "gateway_tasks", ["status"])
        op.create_index("ix_gateway_tasks_approval_id", "gateway_tasks", ["approval_id"])
        op.create_index(
            "ix_gateway_tasks_southbound_task_id",
            "gateway_tasks",
            ["southbound_task_id"],
        )
        op.create_index("ix_gateway_tasks_updated_at", "gateway_tasks", ["updated_at"])

    if not _has_table("gateway_idempotency_keys"):
        op.create_table(
            "gateway_idempotency_keys",
            sa.Column("client_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column(
                "idempotency_key",
                sa.String(length=191),
                primary_key=True,
                nullable=False,
            ),
            sa.Column("task_id", sa.String(length=191), nullable=True),
            sa.Column("request_hash", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_gateway_idempotency_keys_task_id",
            "gateway_idempotency_keys",
            ["task_id"],
        )

    if not _has_table("gateway_task_events"):
        op.create_table(
            "gateway_task_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.String(length=191), nullable=False),
            sa.Column("event_type", sa.String(length=128), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_gateway_task_events_task_id", "gateway_task_events", ["task_id"])
        op.create_index(
            "ix_gateway_task_events_event_type",
            "gateway_task_events",
            ["event_type"],
        )

    if not _has_table("gateway_task_queue"):
        op.create_table(
            "gateway_task_queue",
            sa.Column("task_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("request_json", sa.JSON(), nullable=False),
            sa.Column("request_hash", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("locked_by", sa.String(length=191), nullable=True),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_gateway_task_queue_status", "gateway_task_queue", ["status"])
        op.create_index(
            "ix_gateway_task_queue_next_attempt_at",
            "gateway_task_queue",
            ["next_attempt_at"],
        )
        op.create_index(
            "ix_gateway_task_queue_locked_until",
            "gateway_task_queue",
            ["locked_until"],
        )
        op.create_index("ix_gateway_task_queue_created_at", "gateway_task_queue", ["created_at"])

    if not _has_table("gateway_event_sequences"):
        op.create_table(
            "gateway_event_sequences",
            sa.Column("task_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("last_event_seq", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("gateway_event_outbox"):
        op.create_table(
            "gateway_event_outbox",
            sa.Column("event_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("task_id", sa.String(length=191), nullable=False),
            sa.Column("event_seq", sa.BigInteger(), nullable=False),
            sa.Column("event_type", sa.String(length=128), nullable=False),
            sa.Column("callback_url", sa.String(length=1024), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("locked_by", sa.String(length=191), nullable=True),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_gateway_event_outbox_task_id", "gateway_event_outbox", ["task_id"])
        op.create_index("ix_gateway_event_outbox_status", "gateway_event_outbox", ["status"])
        op.create_index(
            "ix_gateway_event_outbox_next_attempt_at",
            "gateway_event_outbox",
            ["next_attempt_at"],
        )
        op.create_index(
            "ix_gateway_event_outbox_locked_until",
            "gateway_event_outbox",
            ["locked_until"],
        )

    if not _has_table("gateway_webhook_deliveries"):
        op.create_table(
            "gateway_webhook_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.String(length=191), nullable=False),
            sa.Column("callback_url", sa.String(length=1024), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("event_id", "attempt", name="uq_gateway_webhook_delivery_attempt"),
        )
        op.create_index(
            "ix_gateway_webhook_deliveries_event_id",
            "gateway_webhook_deliveries",
            ["event_id"],
        )

    if not _has_table("gateway_dead_letters"):
        op.create_table(
            "gateway_dead_letters",
            sa.Column("event_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("task_id", sa.String(length=191), nullable=False),
            sa.Column("event_seq", sa.BigInteger(), nullable=False),
            sa.Column("event_type", sa.String(length=128), nullable=False),
            sa.Column("callback_url", sa.String(length=1024), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_gateway_dead_letters_task_id", "gateway_dead_letters", ["task_id"])
        op.create_index("ix_gateway_dead_letters_failed_at", "gateway_dead_letters", ["failed_at"])

    if not _has_table("gateway_runtimes"):
        op.create_table(
            "gateway_runtimes",
            sa.Column("runtime_id", sa.String(length=191), nullable=False),
            sa.Column("assistant_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("pod_name", sa.String(length=191), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("capacity", sa.Integer(), nullable=False),
            sa.Column("in_flight", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("capabilities_json", sa.JSON(), nullable=True),
            sa.Column("drain_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_gateway_runtimes_runtime_id", "gateway_runtimes", ["runtime_id"])
        op.create_index("ix_gateway_runtimes_status", "gateway_runtimes", ["status"])
        op.create_index(
            "ix_gateway_runtimes_last_heartbeat_at",
            "gateway_runtimes",
            ["last_heartbeat_at"],
        )

    if not _has_table("gateway_runtime_heartbeats"):
        op.create_table(
            "gateway_runtime_heartbeats",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("assistant_id", sa.String(length=191), nullable=False),
            sa.Column("runtime_id", sa.String(length=191), nullable=True),
            sa.Column("pod_name", sa.String(length=191), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("capacity", sa.Integer(), nullable=False),
            sa.Column("in_flight", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_gateway_runtime_heartbeats_assistant_id",
            "gateway_runtime_heartbeats",
            ["assistant_id"],
        )
        op.create_index(
            "ix_gateway_runtime_heartbeats_received_at",
            "gateway_runtime_heartbeats",
            ["received_at"],
        )


def downgrade() -> None:
    if _has_table("gateway_runtime_heartbeats"):
        op.drop_index(
            "ix_gateway_runtime_heartbeats_received_at",
            table_name="gateway_runtime_heartbeats",
        )
        op.drop_index(
            "ix_gateway_runtime_heartbeats_assistant_id",
            table_name="gateway_runtime_heartbeats",
        )
        op.drop_table("gateway_runtime_heartbeats")

    if _has_table("gateway_runtimes"):
        op.drop_index("ix_gateway_runtimes_last_heartbeat_at", table_name="gateway_runtimes")
        op.drop_index("ix_gateway_runtimes_status", table_name="gateway_runtimes")
        op.drop_index("ix_gateway_runtimes_runtime_id", table_name="gateway_runtimes")
        op.drop_table("gateway_runtimes")

    if _has_table("gateway_dead_letters"):
        op.drop_index("ix_gateway_dead_letters_failed_at", table_name="gateway_dead_letters")
        op.drop_index("ix_gateway_dead_letters_task_id", table_name="gateway_dead_letters")
        op.drop_table("gateway_dead_letters")

    if _has_table("gateway_webhook_deliveries"):
        op.drop_index(
            "ix_gateway_webhook_deliveries_event_id",
            table_name="gateway_webhook_deliveries",
        )
        op.drop_table("gateway_webhook_deliveries")

    if _has_table("gateway_event_outbox"):
        op.drop_index(
            "ix_gateway_event_outbox_next_attempt_at",
            table_name="gateway_event_outbox",
        )
        op.drop_index(
            "ix_gateway_event_outbox_locked_until",
            table_name="gateway_event_outbox",
        )
        op.drop_index("ix_gateway_event_outbox_status", table_name="gateway_event_outbox")
        op.drop_index("ix_gateway_event_outbox_task_id", table_name="gateway_event_outbox")
        op.drop_table("gateway_event_outbox")

    if _has_table("gateway_event_sequences"):
        op.drop_table("gateway_event_sequences")

    if _has_table("gateway_task_events"):
        op.drop_index("ix_gateway_task_events_event_type", table_name="gateway_task_events")
        op.drop_index("ix_gateway_task_events_task_id", table_name="gateway_task_events")
        op.drop_table("gateway_task_events")

    if _has_table("gateway_task_queue"):
        op.drop_index("ix_gateway_task_queue_created_at", table_name="gateway_task_queue")
        op.drop_index("ix_gateway_task_queue_locked_until", table_name="gateway_task_queue")
        op.drop_index("ix_gateway_task_queue_next_attempt_at", table_name="gateway_task_queue")
        op.drop_index("ix_gateway_task_queue_status", table_name="gateway_task_queue")
        op.drop_table("gateway_task_queue")

    if _has_table("gateway_idempotency_keys"):
        op.drop_index(
            "ix_gateway_idempotency_keys_task_id",
            table_name="gateway_idempotency_keys",
        )
        op.drop_table("gateway_idempotency_keys")

    if _has_table("gateway_tasks"):
        op.drop_index("ix_gateway_tasks_updated_at", table_name="gateway_tasks")
        op.drop_index("ix_gateway_tasks_southbound_task_id", table_name="gateway_tasks")
        op.drop_index("ix_gateway_tasks_approval_id", table_name="gateway_tasks")
        op.drop_index("ix_gateway_tasks_status", table_name="gateway_tasks")
        op.drop_index("ix_gateway_tasks_client_id", table_name="gateway_tasks")
        op.drop_table("gateway_tasks")
