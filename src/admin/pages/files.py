"""Admin page: Workspace file management — upload, download, view, delete."""

import streamlit as st

from src.assistants.service import AssistantService
from src.users.service import UserService
from src.workspace_files.models import ExternalPushRequest, WorkspaceFile
from src.workspace_files.service import FileSecurityError, WorkspaceFileService

st.title("Workspace Files")

file_svc = WorkspaceFileService()
assistant_svc = AssistantService()
user_svc = UserService()
assistants = assistant_svc.list_assistants()
assistant_by_id = {assistant.assistant_id: assistant for assistant in assistants}
assistant_ids = sorted(assistant_by_id)

# ── Select user and assistant ────────────────────────────────────────────────

if not assistant_ids:
    st.info("No assistants available. Create an assistant first.")
    st.stop()

selected_user_id = st.text_input("User ID", help="Enter a user ID to load the assistants bound to it.").strip()
if not selected_user_id:
    st.info("Enter a user ID to choose one of its bound assistants before managing files.")
    st.stop()

user = user_svc.get_user(selected_user_id)
if user is None:
    st.warning(f"User `{selected_user_id}` was not found.")
    st.stop()

user_bindings = user_svc.list_bindings_for_user(selected_user_id)
if not user_bindings:
    st.warning(f"User `{selected_user_id}` has no assistant bindings.")
    st.stop()

bound_assistant_ids = [
    binding.assistant_id
    for binding in user_bindings
    if binding.assistant_id in assistant_by_id
]
missing_assistant_ids = sorted(
    {
        binding.assistant_id
        for binding in user_bindings
        if binding.assistant_id not in assistant_by_id
    }
)

if not bound_assistant_ids:
    st.warning(
        f"User `{selected_user_id}` has bindings, but none of the bound assistants exist in the current assistant store."
    )
    if missing_assistant_ids:
        st.caption("Missing assistants: " + ", ".join(missing_assistant_ids))
    st.stop()

def _format_assistant_option(assistant_id: str) -> str:
    assistant = assistant_by_id[assistant_id]
    binding = next(
        (item for item in user_bindings if item.assistant_id == assistant_id),
        None,
    )
    alias = f" - {binding.alias}" if binding and binding.alias else ""
    return f"{assistant_id} ({assistant.name}){alias}"


selected_assistant = st.selectbox(
    "Select assistant",
    bound_assistant_ids,
    format_func=_format_assistant_option,
)

selected_binding = next(
    (binding for binding in user_bindings if binding.assistant_id == selected_assistant),
    None,
)
if selected_binding is not None:
    alias = selected_binding.alias or "no alias"
    source = selected_binding.source_system or "manual"
    st.caption(
        f"Current binding: user `{selected_user_id}` -> assistant `{selected_assistant}` "
        f"(alias: {alias}, source: {source})"
    )

if missing_assistant_ids:
    st.caption("Unavailable bound assistants: " + ", ".join(missing_assistant_ids))

# ── File list ────────────────────────────────────────────────────────────────

st.subheader("Files")
bucket_filter = st.selectbox("Bucket filter", ["All", "temp", "persistent"])
effective_bucket = None if bucket_filter == "All" else bucket_filter
sync_cols = st.columns(2)
if sync_cols[0].button("Scan workspace results", width="stretch"):
    summary = file_svc.scan_workspace_results(
        assistant_id=selected_assistant,
        bucket=effective_bucket,
    )
    if summary.errors:
        st.warning(
            f"Scanned {summary.files_scanned} files, synced {summary.files_synced}, "
            f"deleted {summary.files_deleted}, errors: {len(summary.errors)}"
        )
    else:
        st.success(
            f"Scanned {summary.files_scanned} files, synced {summary.files_synced}, "
            f"deleted {summary.files_deleted}."
        )
    st.rerun()
files = file_svc.list_files(selected_assistant, bucket=effective_bucket)

if not files:
    st.info("No files in this assistant's workspace.")
else:
    st.dataframe(
        [
            {
                "file_id": f.file_id,
                "filename": f.filename,
                "bucket": f.bucket,
                "size_bytes": f.size_bytes,
                "uploader": f.uploader_user_id,
                "source": f.source_type,
                "role": f.file_role,
                "sync_status": f.sync_status,
                "storage_backend": f.storage_backend,
                "object_key": f.object_key,
                "last_synced_at": f.last_synced_at.isoformat() if f.last_synced_at else "",
                "created_at": f.created_at.isoformat(),
            }
            for f in files
        ],
        use_container_width=True,
    )

    # File actions
    selected_file_id = st.selectbox(
        "Select file for actions",
        [f.file_id for f in files],
        format_func=lambda fid: next(
            (f"{f.filename} ({f.bucket})" for f in files if f.file_id == fid), fid
        ),
    )

    action_cols = st.columns(2)
    if action_cols[0].button("Download", width="stretch"):
        result = file_svc.download_file(selected_file_id)
        if result is None:
            st.error("File not found on disk.")
        else:
            record, content = result
            st.download_button(
                label=f"Save {record.filename}",
                data=content,
                file_name=record.filename,
                mime=record.content_type or "application/octet-stream",
            )

    if action_cols[1].button("Delete", width="stretch"):
        result = file_svc.delete_file(selected_file_id, user_id=selected_user_id)
        if result is None:
            st.error("File not found.")
        elif isinstance(result, ExternalPushRequest):
            st.warning(
                f"Persistent file deletion requires approval. "
                f"Approval ID: `{result.approval_id}`"
            )
        else:
            st.success(f"File `{result.filename}` deleted.")
            st.rerun()

# ── Upload ───────────────────────────────────────────────────────────────────

st.subheader("Upload file")
with st.form("upload_file", clear_on_submit=True):
    uploaded = st.file_uploader("Choose file")
    upload_bucket = st.selectbox("Target bucket", ["temp", "persistent"], key="upload_bucket")
    upload_overwrite = st.checkbox("Overwrite if exists")
    upload_source_type = st.text_input("Source type (e.g. oa, email)")
    upload_source_ref = st.text_input("Source reference")
    st.caption(f"Uploading as user `{selected_user_id}` to assistant `{selected_assistant}`.")

    if st.form_submit_button("Upload"):
        if uploaded is None:
            st.warning("Please select a file.")
        else:
            try:
                result = file_svc.upload_file(
                    assistant_id=selected_assistant,
                    bucket=upload_bucket,
                    filename=uploaded.name,
                    content=uploaded.read(),
                    content_type=uploaded.type or "",
                    overwrite=upload_overwrite,
                    source_type=upload_source_type,
                    source_ref=upload_source_ref,
                    uploader_user_id=selected_user_id,
                )
                if isinstance(result, ExternalPushRequest):
                    st.warning(
                        f"Operation requires approval. Approval ID: `{result.approval_id}`"
                    )
                else:
                    st.success(f"File `{result.filename}` uploaded to `{result.bucket}`.")
                    st.rerun()
            except FileSecurityError as exc:
                st.error(f"Security violation: {exc}")
            except ValueError as exc:
                st.error(str(exc))

# ── Recent events ────────────────────────────────────────────────────────────

st.subheader("Recent file events")
events = file_svc.list_events(assistant_id=selected_assistant, limit=50)
if not events:
    st.info("No file events yet.")
else:
    st.dataframe(
        [
            {
                "event_id": e.event_id,
                "action": e.action,
                "file_id": e.file_id,
                "user_id": e.user_id,
                "source_type": e.source_type,
                "detail": str(e.detail),
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ],
        use_container_width=True,
    )

# ── Push request log ─────────────────────────────────────────────────────────

st.subheader("Push request log")
pushes = file_svc.list_push_requests(assistant_id=selected_assistant, limit=50)
if not pushes:
    st.info("No push requests yet.")
else:
    st.dataframe(
        [
            {
                "push_id": p.push_id,
                "filename": p.filename,
                "bucket": p.bucket,
                "status": p.status,
                "user_id": p.user_id,
                "source_type": p.source_type,
                "approval_id": p.approval_id or "",
                "created_at": p.created_at.isoformat(),
            }
            for p in pushes
        ],
        use_container_width=True,
    )
