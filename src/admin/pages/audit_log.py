import streamlit as st

from src.shared.repositories import build_audit_repository

st.title("Audit Log")


def as_record(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}

limit = st.slider("Rows", min_value=10, max_value=200, value=50, step=10)
rows = build_audit_repository().list_recent(limit=limit)

if not rows:
    st.info("No audit events found yet.")
else:
    st.dataframe(
        [
            {
                "request_id": as_record(row.get("envelope")).get("request_id"),
                "hook_name": as_record(row.get("envelope")).get("hook_name"),
                "tool": as_record(row.get("tool")).get("name"),
                "capability_profile": as_record(row.get("assistant")).get("capability_profile"),
                "decision": row.get("decision"),
                "success": as_record(row.get("result")).get("success"),
            }
            for row in rows
        ],
        use_container_width=True,
    )

    with st.expander("Raw records"):
        st.json(rows)
