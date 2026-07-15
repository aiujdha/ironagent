"""Admin page: User management and user-assistant bindings."""

import streamlit as st

from src.assistants.service import AssistantService
from src.users.service import UserService

st.title("Users & Bindings")

user_svc = UserService()
assistant_svc = AssistantService()
users = user_svc.list_users()
assistants = assistant_svc.list_assistants()
assistant_ids = sorted(a.assistant_id for a in assistants)

# ── Metrics ──────────────────────────────────────────────────────────────────

cols = st.columns(3)
cols[0].metric("Users", len(users))
total_bindings = sum(len(user_svc.list_bindings_for_user(u.user_id)) for u in users)
cols[1].metric("Total bindings", total_bindings)
cols[2].metric("Assistants", len(assistants))

# ── Create user ──────────────────────────────────────────────────────────────

st.subheader("Create user")
with st.form("create_user", clear_on_submit=True):
    new_user_id = st.text_input("User ID (employee number)")
    new_display_name = st.text_input("Display name")
    new_employee_id = st.text_input("Employee ID")
    new_department = st.text_input("Department")
    if st.form_submit_button("Create user"):
        if new_user_id.strip():
            try:
                user_svc.create_user(
                    new_user_id.strip(),
                    display_name=new_display_name,
                    employee_id=new_employee_id,
                    department=new_department,
                )
                st.success(f"User `{new_user_id}` created.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        else:
            st.warning("User ID is required.")

# ── User list ────────────────────────────────────────────────────────────────

st.subheader("User list")
if not users:
    st.info("No users registered yet.")
else:
    st.dataframe(
        [
            {
                "user_id": u.user_id,
                "display_name": u.display_name,
                "employee_id": u.employee_id,
                "department": u.department,
                "created_at": u.created_at.isoformat(),
            }
            for u in users
        ],
        use_container_width=True,
    )

# ── User detail & bindings ───────────────────────────────────────────────────

st.subheader("User detail & bindings")
user_options = [u.user_id for u in users]
if not user_options:
    st.info("Create a user first to manage bindings.")
else:
    selected_user_id = st.selectbox("Select user", user_options)
    selected_user = user_svc.get_user(selected_user_id)
    if selected_user:
        st.write(
            {
                "user_id": selected_user.user_id,
                "display_name": selected_user.display_name,
                "employee_id": selected_user.employee_id,
                "department": selected_user.department,
                "created_at": selected_user.created_at.isoformat(),
                "updated_at": selected_user.updated_at.isoformat(),
            }
        )

        bindings = user_svc.list_bindings_for_user(selected_user_id)
        st.markdown("**Bound assistants**")
        if not bindings:
            st.info("No assistants bound to this user.")
        else:
            st.dataframe(
                [
                    {
                        "assistant_id": b.assistant_id,
                        "alias": b.alias,
                        "source_system": b.source_system,
                        "bound_at": b.bound_at.isoformat(),
                    }
                    for b in bindings
                ],
                use_container_width=True,
            )
            # Unbind
            unbind_id = st.selectbox(
                "Select assistant to unbind",
                [b.assistant_id for b in bindings],
                key="unbind_select",
            )
            if st.button("Unbind selected assistant"):
                user_svc.unbind_assistant(selected_user_id, unbind_id)
                st.success(f"Unbound `{unbind_id}` from `{selected_user_id}`.")
                st.rerun()

        # Bind new assistant
        st.markdown("**Bind new assistant**")
        available = [aid for aid in assistant_ids if aid not in {b.assistant_id for b in bindings}]
        if not available:
            st.caption("All assistants are already bound to this user.")
        else:
            with st.form("bind_assistant", clear_on_submit=True):
                bind_assistant_id = st.selectbox("Assistant", available)
                bind_alias = st.text_input("Alias (optional)")
                bind_source = st.text_input("Source system (optional)")
                if st.form_submit_button("Bind"):
                    user_svc.bind_assistant(
                        selected_user_id,
                        bind_assistant_id,
                        alias=bind_alias,
                        source_system=bind_source,
                    )
                    st.success(f"Bound `{bind_assistant_id}` to `{selected_user_id}`.")
                    st.rerun()

        # Delete user
        st.markdown("---")
        if st.button("Delete this user", type="secondary"):
            user_svc.delete_user(selected_user_id)
            st.success(f"User `{selected_user_id}` deleted.")
            st.rerun()
