from collections.abc import Sequence
from typing import Literal

import streamlit as st

from src.approval.models import ApprovalRequest
from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.workspace_files.service import WorkspaceFileService

st.title("Approvals")

service = ApprovalService()
assistant_service = AssistantService()
file_service = WorkspaceFileService()
assistant_ids = sorted(item.assistant_id for item in assistant_service.list_assistants())
assistant_options = ["All assistants", *assistant_ids]


def render_queue(
    title: str,
    items: Sequence[ApprovalRequest],
    *,
    empty_message: str,
    helper: str,
) -> None:
    st.subheader(title)
    st.caption(helper)
    if not items:
        st.info(empty_message)
        return
    for item in items:
        with st.container(border=True):
            st.markdown(f"**{item.tool_name}**")
            st.caption(f"approval_id={item.id}")
            st.write(
                {
                    "assistant_id": item.assistant_id,
                    "capability_profile": item.capability_profile,
                    "approval_scope": item.approval_scope,
                    "reason": item.reason,
                    "target": item.target,
                    "requested_at": item.requested_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                }
            )
            actions = st.columns(2)
            if actions[0].button("Allow", key=f"allow-{item.id}", width="stretch"):
                resolved = service.resolve_request(item.id, "allow")
                if resolved is not None:
                    file_service.apply_approval_resolution(resolved.id)
                st.rerun()
            if actions[1].button("Deny", key=f"deny-{item.id}", width="stretch"):
                resolved = service.resolve_request(item.id, "deny")
                if resolved is not None:
                    file_service.apply_approval_resolution(resolved.id)
                st.rerun()


def normalize_scope(
    value: str,
) -> Literal["user", "admin"] | None:
    if value == "user":
        return "user"
    if value == "admin":
        return "admin"
    return None


filter_columns = st.columns(3)
selected_assistant = filter_columns[0].selectbox("Assistant", assistant_options, index=0)
selected_scope = filter_columns[1].selectbox(
    "Approval scope",
    ["All scopes", "admin"],
    index=0,
)
selected_status = filter_columns[2].selectbox(
    "Status",
    ["All statuses", "pending", "approved", "denied"],
    index=0,
)

assistant_filter = None if selected_assistant == "All assistants" else selected_assistant
scope_filter = normalize_scope(selected_scope)
status_filter = None if selected_status == "All statuses" else selected_status

all_requests = [
    item
    for item in reversed(
        service.list_requests(
            assistant_id=assistant_filter,
            approval_scope=scope_filter,
            status=status_filter,
    )
        )
    if item.approval_scope != "user"  # 过滤掉用户审批
]
pending_user: list = []  # 不再显示用户审批
pending_admin = service.list_pending(approval_scope="admin", assistant_id=assistant_filter)
visible_pending_total = len(pending_admin)  # 只统计管理员审批
approved_count = sum(1 for item in all_requests if item.status == "approved")
denied_count = sum(1 for item in all_requests if item.status == "denied")

metric_columns = st.columns(4)
metric_columns[0].metric("Visible approvals", len(all_requests))
metric_columns[1].metric("Pending", visible_pending_total)
metric_columns[2].metric("Approved", approved_count)
metric_columns[3].metric("Denied", denied_count)

if assistant_filter is not None:
    st.caption(f"Filtering approvals for assistant: {assistant_filter}")

show_pending_queues = status_filter in {None, "pending"}
show_admin_queue = scope_filter in {None, "admin"}

if show_pending_queues:
    if show_admin_queue:
        render_queue(
            "Admin approval queue",
            pending_admin,
            empty_message="No pending admin approvals.",
            helper=(
                "Use this queue for higher-risk actions such as third-party "
                "commands and external operations."
            ),
        )
else:
    st.info("Pending queues are hidden because the current status filter excludes pending items.")

st.subheader("All approval records")
st.dataframe(
    [
        {
            "id": item.id,
            "tool_name": item.tool_name,
            "assistant_id": item.assistant_id,
            "capability_profile": item.capability_profile,
            "approval_scope": item.approval_scope,
            "reason": item.reason,
            "target": item.target,
            "status": item.status,
            "resolution": item.resolution,
            "requested_at": item.requested_at.isoformat(),
            "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        }
        for item in all_requests
    ],
    width="stretch",
)
