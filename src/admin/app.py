import streamlit as st

from src.approval.service import ApprovalService
from src.admin.utils import mask_secret
from src.assistants.service import AssistantService
from src.shared.repositories import build_audit_repository

st.set_page_config(page_title="IronAgent Admin", layout="wide")

st.title("IronAgent Admin")
st.caption("Enterprise safety controls for IronAgent")


def count_audit_events() -> int:
    return build_audit_repository().count_events()


assistant_service = AssistantService()
approval_service = ApprovalService()
assistants = assistant_service.list_assistants()
pending = approval_service.list_pending()
pending_user = approval_service.list_pending(approval_scope="user")
pending_admin = approval_service.list_pending(approval_scope="admin")

metric_columns = st.columns(4)
metric_columns[0].metric("Assistants", len(assistants))
metric_columns[1].metric("Pending approvals", len(pending))
metric_columns[2].metric("Audit events", count_audit_events())
metric_columns[3].metric(
    "Running containers",
    sum(1 for item in assistants if item.runtime.status == "running"),
)
st.caption(f"User approvals: {len(pending_user)} | Admin approvals: {len(pending_admin)}")

st.markdown(
    """
    ### MVP surfaces

    - Capability profiles
    - Approval queue
    - Audit log inspection
    - Assistant inventory
    """
)

st.markdown("### Current assistants")
st.dataframe(
    [
        {
            "assistant_id": item.assistant_id,
            "name": item.name,
            "capability_profile": item.capability_profile,
            "runtime_status": item.runtime.status,
            "container_name": item.runtime.container_name,
            "workspace_root": item.runtime.workspace_root,
            "gateway_url": item.runtime.gateway_url,
            "gateway_token": mask_secret(item.runtime.gateway_token),
            "notes": item.notes,
        }
        for item in assistants
    ],
    width="stretch",
)
