from pathlib import Path

import streamlit as st

from src.assistants.service import AssistantService
from src.market.plugin.models import PluginRecord
from src.market.service import MarketService
from src.market.skill.models import SkillRecord

MarketRecord = SkillRecord | PluginRecord


def _assistant_options(service: AssistantService) -> dict[str, str]:
    return {
        f"{assistant.name} ({assistant.assistant_id})": assistant.assistant_id
        for assistant in service.list_assistants()
    }


def _render_table(records: list[MarketRecord]) -> None:
    st.dataframe(
        [
            {
                "id": record.id,
                "name": record.name,
                "slug": record.slug,
                "version": record.version,
                "status": record.status,
                "install_count": record.install_count,
                "storage_backend": record.storage_backend,
                "package_path": record.package_path,
            }
            for record in records
        ],
        width="stretch",
    )


def _render_upload_form(kind: str, service: MarketService) -> None:
    with st.form(f"{kind}_upload_form", clear_on_submit=True):
        st.subheader(f"Upload {kind.title()[:-1]} Package")
        slug = st.text_input("Slug")
        display_name = st.text_input("Display Name")
        owner = st.text_input("Owner")
        version = st.text_input("Version", value="1.0.0")
        tags = st.text_input("Tags", value="latest")
        uploaded = st.file_uploader(
            "Package archive",
            type=["zip", "tar", "gz", "tgz"],
            key=f"{kind}_uploader",
        )
        submitted = st.form_submit_button("Upload")
        if not submitted:
            return
        if uploaded is None:
            st.error("Please choose an archive file first.")
            return

        content = uploaded.getvalue()
        try:
            if kind == "skills":
                record = service.upload_skill(
                    filename=uploaded.name,
                    content=content,
                    slug=slug,
                    display_name=display_name,
                    owner=owner,
                    version=version,
                    tags=tags,
                )
            else:
                record = service.upload_plugin(
                    filename=uploaded.name,
                    content=content,
                    slug=slug,
                    display_name=display_name,
                    owner=owner,
                    version=version,
                    tags=tags,
                )
        except ValueError as exc:
            st.error(str(exc))
            return

        st.success(f"Uploaded `{record.name}` successfully.")
        st.rerun()


def _render_actions(kind: str, service: MarketService, assistant_options: dict[str, str]) -> None:
    records = service.list_skills() if kind == "skills" else service.list_plugins()
    if not records:
        st.info(f"No {kind} uploaded yet.")
        return

    _render_table(records)
    labels = {record.id: f"{record.name} ({record.slug})" for record in records}
    selected_id = st.selectbox(
        f"Choose a {kind[:-1]}",
        options=list(labels),
        format_func=lambda item_id: labels[item_id],
        key=f"{kind}_selected_id",
    )
    selected_record = next(record for record in records if record.id == selected_id)

    selected_assistant_id = None
    if assistant_options:
        selected_assistant_label = st.selectbox(
            "Install to assistant",
            options=list(assistant_options),
            key=f"{kind}_assistant",
        )
        selected_assistant_id = assistant_options[selected_assistant_label]

    action_columns = st.columns(3)
    if action_columns[0].button("Approve", key=f"{kind}_approve"):
        if kind == "skills":
            service.approve_skill(selected_id)
        else:
            service.approve_plugin(selected_id)
        st.success("Status updated to approved.")
        st.rerun()

    if action_columns[1].button("Install to assistant", key=f"{kind}_install"):
        if selected_assistant_id is None:
            st.error("Create an assistant first.")
        else:
            try:
                if kind == "skills":
                    result = service.install_skill(selected_id, selected_assistant_id)
                else:
                    result = service.install_plugin(selected_id, selected_assistant_id)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(
                    f"Installed into assistant `{selected_assistant_id}` at "
                    f"`{result.install_path}`. Count: {result.install_count}."
                )
                st.info(
                    "You can now open Admin -> Assistants to view the installed list, "
                    "disable or enable the item, or reuse it to another assistant."
                )

    if action_columns[2].button("Delete", key=f"{kind}_delete"):
        if kind == "skills":
            service.delete_skill(selected_id)
        else:
            service.delete_plugin(selected_id)
        st.success("Package deleted.")
        st.rerun()

    st.caption(
        f"Selected archive: `{selected_record.slug}` | "
        f"backend: `{selected_record.storage_backend}` | "
        f"installs: `{selected_record.install_count}`"
    )
    st.caption(
        "Download archive exports the raw package to your browser. "
        "Install to assistant deploys it into the selected assistant workspace."
    )
    try:
        if kind == "skills":
            download_target = service.get_skill_download_target(selected_id)
        else:
            download_target = service.get_plugin_download_target(selected_id)
    except ValueError as exc:
        st.error(str(exc))
        return

    if download_target.source == "local":
        archive_path = Path(download_target.path)
        st.download_button(
            "Download archive",
            data=archive_path.read_bytes(),
            file_name=download_target.filename,
            mime="application/octet-stream",
            key=f"{kind}_download",
            width="stretch",
        )
    else:
        st.link_button(
            "Open download URL",
            download_target.path,
            width="stretch",
        )


st.title("Market")
st.caption("Internal-only skills and plugins for assistant workspaces.")

assistant_service = AssistantService()
market_service = MarketService()
assistant_options = _assistant_options(assistant_service)

skill_tab, plugin_tab = st.tabs(["Skills", "Plugins"])

with skill_tab:
    st.write("Uploaded skills can be installed into an assistant workspace `skills/` directory.")
    _render_upload_form("skills", market_service)
    _render_actions("skills", market_service, assistant_options)

with plugin_tab:
    st.write("Uploaded plugins can be installed into an assistant workspace `plugins/` directory.")
    _render_upload_form("plugins", market_service)
    _render_actions("plugins", market_service, assistant_options)
