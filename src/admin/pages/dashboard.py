import streamlit as st

from src.approval.service import ApprovalService
from src.admin.utils import mask_secret
from src.assistants.service import AssistantService
from src.shared.repositories import build_audit_repository

st.title("Dashboard")


def as_record(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def load_recent_audit_rows(limit: int = 10) -> list[dict[str, object]]:
    return build_audit_repository().list_recent(limit=limit)


assistant_service = AssistantService()
approval_service = ApprovalService()
assistants = assistant_service.list_assistants()
pending = approval_service.list_pending()
pending_user = approval_service.list_pending(approval_scope="user")
pending_admin = approval_service.list_pending(approval_scope="admin")
recent_audits = load_recent_audit_rows()

metrics = st.columns(3)
metrics[0].metric("Assistants", len(assistants))
metrics[1].metric("Pending approvals", len(pending))
metrics[2].metric("Recent audit events", len(recent_audits))
st.caption(f"User approvals: {len(pending_user)} | Admin approvals: {len(pending_admin)}")

runtime_counts: dict[str, int] = {}
for assistant in assistants:
    runtime_counts[assistant.runtime.status] = runtime_counts.get(assistant.runtime.status, 0) + 1

st.subheader("Assistant runtime overview")
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
        }
        for item in assistants
    ],
    width="stretch",
)

if runtime_counts:
    st.caption(
        ", ".join(f"{status}: {count}" for status, count in sorted(runtime_counts.items()))
    )

st.subheader("Latest audit events")
if recent_audits:
    st.dataframe(
        [
            {
                "request_id": as_record(row.get("envelope")).get("request_id"),
                "tool": as_record(row.get("tool")).get("name"),
                "decision": row.get("decision"),
                "capability_profile": as_record(row.get("assistant")).get("capability_profile"),
                "success": as_record(row.get("result")).get("success"),
            }
            for row in recent_audits
        ],
        width="stretch",
    )
else:
    st.info("No audit events yet.")
