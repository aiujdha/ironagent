"""User-facing file portal for uploads, results, and downloads."""

from __future__ import annotations

from collections import defaultdict

import streamlit as st

from src.assistants.service import AssistantService
from src.users.service import UserService
from src.workspace_files.models import ExternalPushRequest, WorkspaceFile
from src.workspace_files.service import FileSecurityError, WorkspaceFileService

st.set_page_config(page_title="File Portal", layout="wide")

file_svc = WorkspaceFileService()
user_svc = UserService()
assistant_svc = AssistantService()
assistants = {assistant.assistant_id: assistant for assistant in assistant_svc.list_assistants()}


STATUS_LABELS = {
    "synced": "Ready",
    "disabled": "Workspace only",
    "failed": "Sync failed",
    "pending": "Pending",
}



def _assistant_label(assistant_id: str, binding_map: dict[str, object]) -> str:
    assistant = assistants[assistant_id]
    binding = binding_map.get(assistant_id)
    alias = f" / {binding.alias}" if binding and getattr(binding, "alias", "") else ""
    return f"{assistant.name} ({assistant_id}){alias}"



def _storage_label(file: WorkspaceFile) -> str:
    return STATUS_LABELS.get(file.storage_status, file.storage_status.title())



def _download_block(record: WorkspaceFile, *, label: str, key_prefix: str) -> None:
    downloaded = file_svc.download_file(record.file_id)
    if downloaded is None:
        st.warning("Download unavailable")
        return
    item, content = downloaded
    st.download_button(
        label=label,
        data=content,
        file_name=item.filename,
        mime=item.content_type or "application/octet-stream",
        key=f"{key_prefix}-{item.file_id}",
        use_container_width=True,
    )


st.title("File Portal")
st.caption("Upload files, track processing, and download assistant results.")

selected_user_id = st.text_input(
    "User ID",
    help="Enter your enterprise user ID to load your assistants and files.",
).strip()

if not selected_user_id:
    st.info("Enter your user ID to continue.")
    st.stop()

user = user_svc.get_user(selected_user_id)
if user is None:
    st.error(f"User `{selected_user_id}` was not found.")
    st.stop()

bindings = user_svc.list_bindings_for_user(selected_user_id)
assistant_ids = [binding.assistant_id for binding in bindings if binding.assistant_id in assistants]
if not assistant_ids:
    st.warning("No assistants are currently bound to this user.")
    st.stop()

binding_map = {binding.assistant_id: binding for binding in bindings}

control_left, control_right = st.columns([2.2, 1])
with control_left:
    selected_assistant_id = st.selectbox(
        "Assistant",
        assistant_ids,
        format_func=lambda assistant_id: _assistant_label(assistant_id, binding_map),
    )
with control_right:
    st.write("")
    if st.button("Refresh now", use_container_width=True):
        st.rerun()

selected_assistant = assistants[selected_assistant_id]
files = file_svc.list_files(selected_assistant_id, include_deleted=False)
input_files = sorted(
    [item for item in files if item.file_role == "input"],
    key=lambda item: item.updated_at,
    reverse=True,
)
result_files = sorted(
    [item for item in files if item.file_role == "result"],
    key=lambda item: item.updated_at,
    reverse=True,
)

source_to_results: dict[str, list[WorkspaceFile]] = defaultdict(list)
orphan_results: list[WorkspaceFile] = []
for item in result_files:
    if item.source_file_id:
        source_to_results[item.source_file_id].append(item)
    else:
        orphan_results.append(item)

metrics = st.columns(5)
metrics[0].metric("Inputs", len(input_files))
metrics[1].metric("Results", len(result_files))
metrics[2].metric("Ready", sum(1 for item in files if item.storage_status == "synced"))
metrics[3].metric("Workspace only", sum(1 for item in files if item.storage_status == "disabled"))
metrics[4].metric("Sync failed", sum(1 for item in files if item.storage_status == "failed"))

hero_left, hero_right = st.columns([1.5, 1])
with hero_left:
    st.markdown(f"### {selected_assistant.name}")
    st.caption(
        f"Assistant ID: `{selected_assistant.assistant_id}` | "
        f"Runtime: `{selected_assistant.runtime.status}` | "
        f"Gateway: `{selected_assistant.runtime.gateway_url or 'not available'}`"
    )
with hero_right:
    st.info(
        "Uploads land in the assistant workspace first. Result files are discovered automatically by the background scanner and synced to object storage when available."
    )

st.markdown("### Upload")
with st.form("user_file_upload", clear_on_submit=True):
    upload = st.file_uploader("Choose a file")
    upload_bucket = st.segmented_control(
        "Target folder",
        options=["temp", "persistent"],
        default="temp",
        selection_mode="single",
    )
    upload_overwrite = st.checkbox("Overwrite existing file")
    upload_source_type = st.text_input("Source type", value="webui")
    upload_source_ref = st.text_input("Source ref", value=selected_user_id)
    submitted = st.form_submit_button("Upload")
    if submitted:
        if upload is None:
            st.warning("Choose a file first.")
        else:
            try:
                result = file_svc.upload_file(
                    assistant_id=selected_assistant_id,
                    bucket=upload_bucket or "temp",
                    filename=upload.name,
                    content=upload.read(),
                    content_type=upload.type or "application/octet-stream",
                    overwrite=upload_overwrite,
                    source_type=upload_source_type,
                    source_ref=upload_source_ref,
                    uploader_user_id=selected_user_id,
                )
                if isinstance(result, ExternalPushRequest):
                    st.warning(
                        f"Upload is pending approval. Approval ID: `{result.approval_id}`"
                    )
                else:
                    st.success(
                        f"Uploaded `{result.filename}` to `{result.bucket}`. Storage status: `{result.storage_status}`"
                    )
                    st.rerun()
            except FileSecurityError as exc:
                st.error(f"Security policy rejected the upload: {exc}")
            except ValueError as exc:
                st.error(str(exc))

st.markdown("### Results")
result_filter_cols = st.columns([1, 1, 2])
with result_filter_cols[0]:
    selected_result_bucket = st.selectbox("Bucket", ["All", "temp", "persistent"])
with result_filter_cols[1]:
    selected_result_status = st.selectbox(
        "Status",
        ["All", "Ready", "Workspace only", "Sync failed", "Pending"],
    )
with result_filter_cols[2]:
    result_search = st.text_input("Search results", placeholder="Filename contains...").strip().lower()

filtered_results = result_files
if selected_result_bucket != "All":
    filtered_results = [item for item in filtered_results if item.bucket == selected_result_bucket]
if selected_result_status != "All":
    filtered_results = [item for item in filtered_results if _storage_label(item) == selected_result_status]
if result_search:
    filtered_results = [item for item in filtered_results if result_search in item.filename.lower()]

if not filtered_results:
    st.info("No result files match the current filters.")
else:
    for result in filtered_results[:20]:
        linked_source = next((item for item in input_files if item.file_id == result.source_file_id), None)
        with st.container(border=True):
            top_left, top_right = st.columns([2.6, 1])
            with top_left:
                st.markdown(f"**{result.filename}**")
                st.caption(
                    f"{result.relative_path} | {_storage_label(result)} | updated: {result.updated_at.isoformat()}"
                )
                if linked_source is not None:
                    st.caption(f"From input: `{linked_source.filename}`")
                elif result.source_type:
                    st.caption(f"Source: `{result.source_type}` / `{result.source_ref}`")
                if result.storage_path:
                    st.caption(f"Object storage: {result.storage_path}")
            with top_right:
                _download_block(result, label="Download result", key_prefix="download-result")

if orphan_results:
    with st.expander(f"Orphan results ({len(orphan_results)})"):
        st.dataframe(
            [
                {
                    "filename": item.filename,
                    "bucket": item.bucket,
                    "status": _storage_label(item),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in orphan_results
            ],
            use_container_width=True,
            hide_index=True,
        )

st.markdown("### Input Timeline")
input_filter = st.text_input("Search inputs", placeholder="Filename contains...").strip().lower()
filtered_inputs = input_files if not input_filter else [
    item for item in input_files if input_filter in item.filename.lower()
]

if not filtered_inputs:
    st.info("No uploaded input files match the current filters.")
else:
    for input_file in filtered_inputs[:30]:
        related_results = sorted(
            source_to_results.get(input_file.file_id, []),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        with st.container(border=True):
            head_left, head_right = st.columns([2.2, 1])
            with head_left:
                st.markdown(f"**{input_file.filename}**")
                st.caption(
                    f"bucket: `{input_file.bucket}` | {_storage_label(input_file)} | uploaded: {input_file.created_at.isoformat()}"
                )
                st.caption(
                    f"Source: `{input_file.source_type or 'webui'}` | Ref: `{input_file.source_ref or selected_user_id}`"
                )
            with head_right:
                _download_block(input_file, label="Download input", key_prefix="download-input")
            if related_results:
                st.caption(f"Linked results: {len(related_results)}")
                for result in related_results[:5]:
                    row_left, row_right = st.columns([2.3, 1])
                    with row_left:
                        st.write(f"{result.filename} ? {_storage_label(result)}")
                    with row_right:
                        _download_block(result, label="Download", key_prefix=f"linked-result-{input_file.file_id}")
            else:
                st.caption("No linked results yet.")

with st.expander("Recent activity"):
    events = file_svc.list_events(assistant_id=selected_assistant_id, limit=40)
    if not events:
        st.info("No recent events.")
    else:
        st.dataframe(
            [
                {
                    "action": event.action,
                    "file_id": event.file_id,
                    "detail": str(event.detail),
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ],
            use_container_width=True,
            hide_index=True,
        )
