import time
from typing import cast

import streamlit as st

from src.admin.control_ui_launch import mint_control_ui_launch_ticket
from src.admin.display_paths import format_display_path
from src.admin.gateway_links import (
    build_go_gateway_assistant_ui_url,
    build_spdbpanda_chat_launch_url,
)
from src.admin.utils import mask_secret
from src.assistants.models import CAPABILITY_PROFILES
from src.assistants.service import AssistantService, AssistantWorkspaceItem
from src.runtime.models import (
    AssistantAuthMode,
    AssistantIronAgentConfig,
    AssistantWorkspaceConfig,
)
from src.shared.config import get_settings
from src.shared.workspace_roots import WorkspaceRootEntry, list_workspace_roots

RUNTIME_AUTO_REFRESH_ASSISTANT_KEY = "assistant_runtime_auto_refresh_assistant"
RUNTIME_AUTO_REFRESH_UNTIL_KEY = "assistant_runtime_auto_refresh_until"
RUNTIME_LIST_AUTO_REFRESH_UNTIL_KEY = "assistant_runtime_list_auto_refresh_until"


def _arm_runtime_auto_refresh(assistant_id: str, *, seconds: int = 20) -> None:
    st.session_state[RUNTIME_AUTO_REFRESH_ASSISTANT_KEY] = assistant_id
    st.session_state[RUNTIME_AUTO_REFRESH_UNTIL_KEY] = time.time() + seconds


def _clear_runtime_auto_refresh() -> None:
    st.session_state.pop(RUNTIME_AUTO_REFRESH_ASSISTANT_KEY, None)
    st.session_state.pop(RUNTIME_AUTO_REFRESH_UNTIL_KEY, None)


def _arm_runtime_list_auto_refresh(*, seconds: int = 30) -> None:
    st.session_state[RUNTIME_LIST_AUTO_REFRESH_UNTIL_KEY] = time.time() + seconds


def _clear_runtime_list_auto_refresh() -> None:
    st.session_state.pop(RUNTIME_LIST_AUTO_REFRESH_UNTIL_KEY, None)


def _render_gateway_credentials(
    gateway_url: str,
    gateway_token: str,
    *,
    url_key: str,
    token_key: str,
) -> None:
    st.text_input("Gateway URL", value=gateway_url, disabled=True, key=url_key)
    st.text_input(
        "Gateway Token",
        value=gateway_token,
        type="password",
        disabled=True,
        key=token_key,
    )


def _render_gateway_launch_button(
    gateway_url: str | None,
    gateway_token: str | None,
    *,
    assistant_id: str | None = None,
    label: str = "一键打开助手",
    url_key: str = "assistant_access_gateway_url",
    token_key: str = "assistant_access_gateway_token",
) -> None:
    settings = get_settings()
    launch_ticket = None
    if gateway_url and gateway_token and (
        settings.go_gateway_public_url.strip() or settings.security_service_public_url.strip()
    ):
        launch_ticket = mint_control_ui_launch_ticket(
            assistant_id=assistant_id,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            session_key="main",
        )
    go_gateway_launch_url = build_go_gateway_assistant_ui_url(
        go_gateway_public_url=settings.go_gateway_public_url,
        assistant_id=assistant_id,
        launch_ticket=launch_ticket.ticket if launch_ticket else None,
    )
    launch_url = (
        go_gateway_launch_url
        or
        build_spdbpanda_chat_launch_url(
            control_ui_url=settings.control_ui_public_url,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            session_key="main",
            security_service_url=settings.security_service_public_url,
            launch_ticket=launch_ticket.ticket if launch_ticket else None,
        )
        or gateway_url
    )
    if not gateway_url:
        st.caption("Gateway URL / Token unavailable.")
        return
    st.link_button(label, launch_url, width="stretch")
    if gateway_token:
        if launch_ticket:
            st.caption(
                "按钮会打开 SPDBPanda 聊天页；浏览器先用一次性 launch ticket 换取当前连接参数，"
                "原始 gateway token 不再直接出现在入口链接中。"
            )
        elif settings.control_ui_public_url:
            st.caption(
                "按钮会打开 SPDBPanda 聊天页；gateway token 与文件上传 token "
                "只通过 URL fragment 传入，并会被页面立刻清理到当前标签页会话。"
            )
        else:
            st.caption(
                "尚未配置 IRON_AGENT_CONTROL_UI_PUBLIC_URL，当前仍回退到助手原始 gateway 入口。"
                ""
            )
        with st.expander("调试信息：原始 gateway 凭据", expanded=False):
            _render_gateway_credentials(
                gateway_url,
                gateway_token,
                url_key=url_key,
                token_key=token_key,
            )
    else:
        st.caption("Gateway token unavailable. Open the dashboard and paste the token manually.")


def _render_workspace_item_manager(
    *,
    service: AssistantService,
    selected_id: str,
    items: list[AssistantWorkspaceItem],
    assistants: list,
    kind: str,
) -> None:
    label = "skill" if kind == "skills" else "plugin"
    kind_items = [item for item in items if item.kind == kind]
    if not kind_items:
        st.info(f"This assistant has no installed {kind} yet.")
        return

    st.dataframe(
        [
            {
                "slug": item.slug,
                "name": item.name,
                "version": item.version,
                "status": "enabled" if item.enabled else "disabled",
                "install_path": format_display_path(item.install_path),
            }
            for item in kind_items
        ],
        width="stretch",
    )
    selected_slug = st.selectbox(
        f"Choose installed {label}",
        options=[item.slug for item in kind_items],
        format_func=lambda slug: next(
            (
                f"{item.name} ({item.slug})"
                for item in kind_items
                if item.slug == slug
            ),
            slug,
        ),
        key=f"{selected_id}_{kind}_installed_item",
    )
    selected_item = next(item for item in kind_items if item.slug == selected_slug)
    st.caption(
        f"Current status: {'enabled' if selected_item.enabled else 'disabled'} | "
        f"path: {format_display_path(selected_item.install_path)}"
    )

    target_assistants = [item for item in assistants if item.assistant_id != selected_id]
    target_options = {
        f"{item.name} ({item.assistant_id})": item.assistant_id for item in target_assistants
    }
    if target_options:
        target_label = st.selectbox(
            "Reuse to assistant",
            options=list(target_options),
            key=f"{selected_id}_{kind}_reuse_target",
        )
        target_assistant_id = target_options[target_label]
    else:
        target_assistant_id = None
        st.caption("No other assistant is available for reuse.")

    action_cols = st.columns(2)
    toggle_label = "Disable item" if selected_item.enabled else "Enable item"
    if action_cols[0].button(toggle_label, key=f"{selected_id}_{kind}_toggle", width="stretch"):
        updated = service.set_workspace_item_enabled(
            assistant_id=selected_id,
            kind=kind,
            slug=selected_item.slug,
            enabled=not selected_item.enabled,
        )
        st.success(
            f"{updated.kind[:-1].title()} `{updated.slug}` is now "
            f"{'enabled' if updated.enabled else 'disabled'}."
        )
        st.rerun()

    if action_cols[1].button(
        "Reuse to selected assistant",
        key=f"{selected_id}_{kind}_reuse",
        width="stretch",
        disabled=target_assistant_id is None,
    ):
        reused = service.reuse_workspace_item(
            source_assistant_id=selected_id,
            target_assistant_id=cast(str, target_assistant_id),
            kind=kind,
            slug=selected_item.slug,
        )
        st.success(
            f"Reused `{reused.slug}` to assistant `{reused.target_assistant_id}` at "
            f"`{format_display_path(reused.install_path)}`."
        )
        st.rerun()


def describe_host_auth_status() -> tuple[str, list[str]]:
    settings = get_settings()
    host_root = settings.host_iron_agent_root
    agent_root = host_root / "agents" / settings.host_iron_agent_agent_id / "agent"
    checks = {
        "宿主机 SPDBPanda 运行配置": host_root / "ironagent.json",
        "宿主机 auth-profiles.json": agent_root / "auth-profiles.json",
        "宿主机 models.json": agent_root / "models.json",
    }
    missing = [label for label, path in checks.items() if not path.exists()]
    if missing:
        return (
            "missing",
            [
                f"认证目录：{format_display_path(host_root)}",
                f"Agent ID：{settings.host_iron_agent_agent_id}",
                *[f"缺少：{label}" for label in missing],
            ],
        )
    return (
        "ready",
        [
            f"认证目录：{format_display_path(host_root)}",
            f"Agent ID：{settings.host_iron_agent_agent_id}",
            "宿主机认证材料已就绪，可用于 openai-codex 模型。",
        ],
    )

st.title("Assistants")

service = AssistantService()
assistants, runtime_probes, runtime_synced_count = service.sync_runtime_views()
default_iron_agent_config = AssistantIronAgentConfig()
default_workspace_config = AssistantWorkspaceConfig()
assistant_options = ["__new__"] + [item.assistant_id for item in assistants]
selected_id = st.selectbox(
    "Choose an assistant to edit",
    options=assistant_options,
    format_func=lambda value: "Create new assistant" if value == "__new__" else value,
)

selected = next((item for item in assistants if item.assistant_id == selected_id), None)
default_profile = (
    CAPABILITY_PROFILES.index(selected.capability_profile)
    if selected
    else CAPABILITY_PROFILES.index("onlyread")
)
default_auth_mode = (
    selected.iron_agent_config.auth_mode if selected else default_iron_agent_config.auth_mode
)
workspace_settings = get_settings()
try:
    workspace_roots = list_workspace_roots(workspace_settings)
    workspace_root_error = ""
except ValueError as exc:
    workspace_roots = [
        WorkspaceRootEntry(
            id=workspace_settings.default_workspace_root_id,
            label="默认工作空间",
            host_path=workspace_settings.assistants_workspace_root,
        )
    ]
    workspace_root_error = str(exc)
selected_workspace_root_id = (
    selected.workspace_config.root_id if selected else default_workspace_config.root_id
)
workspace_root_ids = [entry.id for entry in workspace_roots]
if selected_workspace_root_id not in workspace_root_ids:
    selected_workspace_root_id = workspace_root_ids[0]
default_workspace_index = workspace_root_ids.index(selected_workspace_root_id)
default_workspace_slug = (
    selected.workspace_config.workspace_slug
    if selected and selected.workspace_config.workspace_slug
    else (selected.assistant_id if selected else "")
)
default_auto_place_outputs = (
    selected.workspace_config.auto_place_outputs
    if selected
    else default_workspace_config.auto_place_outputs
)
host_auth_status, host_auth_lines = describe_host_auth_status()

auto_refresh_assistant_id = cast(
    str | None,
    st.session_state.get(RUNTIME_AUTO_REFRESH_ASSISTANT_KEY),
)
auto_refresh_until = cast(
    float | None,
    st.session_state.get(RUNTIME_AUTO_REFRESH_UNTIL_KEY),
)
if selected is not None and auto_refresh_assistant_id == selected.assistant_id:
    if selected.runtime.status != "starting":
        _clear_runtime_auto_refresh()
    elif auto_refresh_until is not None and time.time() < auto_refresh_until:
        st.info("助手正在启动，页面将在约 2 秒后自动刷新一次。")
        time.sleep(2)
        st.rerun()
    else:
        _clear_runtime_auto_refresh()

starting_assistant_ids = [
    item.assistant_id for item in assistants if item.runtime.status == "starting"
]
list_auto_refresh_until = cast(
    float | None,
    st.session_state.get(RUNTIME_LIST_AUTO_REFRESH_UNTIL_KEY),
)
if starting_assistant_ids:
    if list_auto_refresh_until is None:
        _arm_runtime_list_auto_refresh()
        list_auto_refresh_until = cast(
            float | None,
            st.session_state.get(RUNTIME_LIST_AUTO_REFRESH_UNTIL_KEY),
        )
    if list_auto_refresh_until is not None and time.time() < list_auto_refresh_until:
        if selected is not None and selected.assistant_id in starting_assistant_ids:
            st.info("当前助手仍在启动中，页面将在约 2 秒后自动刷新一次。")
        else:
            st.info(
                f"检测到 {len(starting_assistant_ids)} 个助手正在启动，"
                "页面将在约 2 秒后自动刷新一次。"
            )
        time.sleep(2)
        st.rerun()
    _clear_runtime_list_auto_refresh()
else:
    _clear_runtime_list_auto_refresh()

if runtime_synced_count:
    st.caption(f"已自动同步 {runtime_synced_count} 个助手的运行时状态。")

st.subheader("Model authentication")
auth_col, status_col = st.columns([1, 1.4])
with auth_col:
    st.caption("当前支持两种方式：宿主机 SPDBPanda 兼容认证，或单助手 Provider API Key。")
with status_col:
    if host_auth_status == "ready":
        st.success("\n".join(host_auth_lines))
    else:
        st.warning("\n".join(host_auth_lines))
if workspace_root_error:
    st.warning(f"工作空间根目录配置存在问题，当前回退到默认根目录：{workspace_root_error}")

auth_mode_state_key = "assistant_form_auth_mode"
auth_mode_context_key = "assistant_form_auth_mode_context"
if st.session_state.get(auth_mode_context_key) != selected_id:
    st.session_state[auth_mode_state_key] = default_auth_mode
    st.session_state[auth_mode_context_key] = selected_id

auth_mode = cast(
    AssistantAuthMode,
    st.radio(
        "Authentication source",
        options=["host_iron_agent", "provider_api_key"],
        index=0 if default_auth_mode == "host_iron_agent" else 1,
        format_func=lambda value: (
            "宿主机 SPDBPanda 兼容认证（当前推荐给 openai-codex）"
            if value == "host_iron_agent"
            else "Provider API Key（适合 modelstudio / zai / ollama 等）"
        ),
        help="选择助手使用宿主机认证，还是为当前助手单独保存 Provider API Key。",
        key=auth_mode_state_key,
    ),
)
if auth_mode == "host_iron_agent":
    st.info(
        "宿主机兼容认证当前主要用于 openai-codex。启用后会自动同步宿主机 "
        "SPDBPanda 认证目录中的登录态到助手容器，并忽略下方 Provider API Key 字段。"
    )
else:
    st.info("Provider API Key 会保存到助手配置中，并物化到对应运行时配置文件。")

with st.form("assistant-form"):
    assistant_id = st.text_input("Assistant ID", value=selected.assistant_id if selected else "")
    name = st.text_input("Display name", value=selected.name if selected else "")
    capability_profile = st.selectbox(
        "Capability profile",
        options=list(CAPABILITY_PROFILES),
        index=default_profile,
    )
    notes = st.text_area("Notes", value=selected.notes if selected else "")
    workspace_root_id = st.selectbox(
        "Workspace root",
        options=workspace_root_ids,
        index=default_workspace_index,
        format_func=lambda value: next(
            (
                f"{entry.label} ({format_display_path(entry.host_path)})"
                for entry in workspace_roots
                if entry.id == value
            ),
            value,
        ),
        help="为当前助手选择公共工作空间根目录。",
    )
    workspace_slug = st.text_input(
        "Workspace directory name",
        value=default_workspace_slug,
        help="工作空间目录名。留空时会自动使用 Assistant ID。",
    )
    auto_place_outputs = st.checkbox(
        "Auto place outputs",
        value=default_auto_place_outputs,
        help="为后续 temp / persistent 自动落点能力预留，当前阶段先记录配置。",
    )
    primary_model = st.text_input(
        "Primary model",
        value=(
            selected.iron_agent_config.primary_model
            if selected
            else default_iron_agent_config.primary_model
        ),
        help="例如：openai-codex/gpt-5.4、modelstudio/qwen3-coder-next",
    )
    provider_id = st.text_input(
        "Provider ID",
        value=(
            selected.iron_agent_config.provider_id
            if selected and default_auth_mode == "provider_api_key"
            else ""
        ),
        help="例如：modelstudio、zai、huggingface",
        disabled=auth_mode == "host_iron_agent",
    )
    provider_base_url = st.text_input(
        "Provider base URL",
        value=(
            selected.iron_agent_config.provider_base_url
            if selected and default_auth_mode == "provider_api_key"
            else ""
        ),
        help="留空则使用默认提供方地址。",
        disabled=auth_mode == "host_iron_agent",
    )
    provider_api_key = st.text_input(
        "Provider API Key",
        value="",
        type="password",
        help="留空则保留当前已保存的 API Key。",
        disabled=auth_mode == "host_iron_agent",
    )
    submitted = st.form_submit_button("Save assistant")

if submitted:
    if auth_mode == "host_iron_agent" and not primary_model.strip().startswith("openai-codex/"):
        st.warning(
            "宿主机认证当前主要面向 openai-codex。"
            "非 openai-codex 模型建议改用 Provider API Key。"
        )
    saved = service.upsert_assistant(
        assistant_id=assistant_id,
        name=name,
        capability_profile=capability_profile,
        notes=notes,
        model_config=AssistantIronAgentConfig(
            primary_model=primary_model,
            auth_mode=auth_mode,
            provider_id=provider_id if auth_mode == "provider_api_key" else "",
            provider_base_url=provider_base_url if auth_mode == "provider_api_key" else "",
            provider_api_key=provider_api_key if auth_mode == "provider_api_key" else "",
        ),
        workspace_config=AssistantWorkspaceConfig(
            selection_mode=(
                "default_root"
                if workspace_root_id == workspace_settings.default_workspace_root_id
                else "managed_root"
            ),
            root_id=workspace_root_id,
            workspace_slug=workspace_slug,
            auto_place_outputs=auto_place_outputs,
        ),
    )
    if saved.runtime.status == "failed":
        st.warning(
            f"Saved assistant '{assistant_id}', but runtime provisioning failed: "
            f"{saved.runtime.last_error or 'unknown error'}"
        )
    else:
        st.success(
            f"Saved assistant '{assistant_id}' with runtime status '{saved.runtime.status}'."
        )
        if saved.runtime.status == "starting":
            _arm_runtime_auto_refresh(saved.assistant_id)
            _arm_runtime_list_auto_refresh()
        if saved.runtime.gateway_url and saved.runtime.gateway_token:
            st.info(
                "助手容器已自动启动。页面刷新后，可在下方“Assistant access”区域进入 "
                "SPDBPanda 聊天页；原始 gateway 凭据默认不再直接展开。"
            )
        else:
            st.warning("运行时已保存，但未获取到网关地址或令牌，请点击下方“修复当前选中助手”。")
    st.rerun()

st.subheader("Runtime operations")
ops_col1, ops_col2 = st.columns(2)
with ops_col1:
    if st.button("实时探活刷新", width="stretch"):
        st.rerun()
with ops_col2:
    if st.button("一键修复运行时状态", width="stretch"):
        repaired, skipped = service.repair_out_of_sync_runtimes()
        st.success(f"修复完成：repaired={repaired}, skipped={skipped}")
        st.rerun()

if selected is not None:
    selected_ops_col1, selected_ops_col2 = st.columns(2)
    with selected_ops_col1:
        if st.button("修复当前选中助手", width="stretch"):
            repaired = service.repair_runtime_state(selected.assistant_id)
            if repaired is None:
                st.warning(f"Assistant '{selected.assistant_id}' not found.")
            elif repaired.runtime.status == "failed":
                st.warning(
                    f"Repair failed for '{selected.assistant_id}': "
                    f"{repaired.runtime.last_error or 'unknown error'}"
                )
            else:
                st.success(
                    f"Repair ok for '{selected.assistant_id}', status={repaired.runtime.status}."
                )
                if repaired.runtime.status == "starting":
                    _arm_runtime_auto_refresh(repaired.assistant_id)
                    _arm_runtime_list_auto_refresh()
                if repaired.runtime.gateway_url and repaired.runtime.gateway_token:
                    st.info(
                        "修复后的连接入口已更新。页面刷新后，可在下方“Assistant access”区域打开 "
                        "SPDBPanda 聊天页；原始 gateway 凭据仅保留在调试折叠区。"
                    )
            st.rerun()
    with selected_ops_col2:
        if st.button("重启当前选中助手", width="stretch"):
            restarted = service.restart_assistant(selected.assistant_id)
            if restarted is None:
                st.warning(f"Assistant '{selected.assistant_id}' not found.")
            elif restarted.runtime.status == "failed":
                st.warning(
                    f"Restart failed for '{selected.assistant_id}': "
                    f"{restarted.runtime.last_error or 'unknown error'}"
                )
            else:
                st.success(
                    f"Restart triggered for '{selected.assistant_id}', "
                    f"status={restarted.runtime.status}."
                )
                if restarted.runtime.status == "starting":
                    _arm_runtime_auto_refresh(restarted.assistant_id)
                    _arm_runtime_list_auto_refresh()
                if restarted.runtime.gateway_url and restarted.runtime.gateway_token:
                    st.info(
                        "重启后的连接入口已更新。页面刷新后，可在下方“Assistant access”区域打开 "
                        "SPDBPanda 聊天页；原始 gateway 凭据仅保留在调试折叠区。"
                    )
            st.rerun()

if selected is not None:
    st.subheader("Assistant access")
    selected_probe = runtime_probes.get(selected.assistant_id)
    selected_gateway_url = (
        selected_probe.gateway_url
        if selected_probe and selected_probe.gateway_url
        else selected.runtime.gateway_url
    )
    _render_gateway_launch_button(
        selected_gateway_url,
        selected.runtime.gateway_token,
        assistant_id=selected.assistant_id,
        label="打开 SPDBPanda 聊天页",
    )

st.subheader("Installed skills and plugins")
if selected is None:
    st.caption("请先在上方选择一个现有助手，才能查看和管理它的 skills / plugins。")
else:
    st.caption(
        "Market 页的 “Install to assistant” 会把包直接装配到当前助手工作空间。"
        "这里可以查看该助手自己的已装列表，并执行停用、启用或复用。"
    )
    try:
        workspace_items = service.list_workspace_items(selected.assistant_id)
    except ValueError as exc:
        st.error(f"无法读取助手工作空间：{exc}")
    else:
        skill_items = [item for item in workspace_items if item.kind == "skills"]
        plugin_items = [item for item in workspace_items if item.kind == "plugins"]
        st.caption(
            f"Installed skills: {len(skill_items)} | Installed plugins: {len(plugin_items)}"
        )
        skill_tab, plugin_tab = st.tabs(["Installed Skills", "Installed Plugins"])
        with skill_tab:
            _render_workspace_item_manager(
                service=service,
                selected_id=selected.assistant_id,
                items=workspace_items,
                assistants=assistants,
                kind="skills",
            )
        with plugin_tab:
            _render_workspace_item_manager(
                service=service,
                selected_id=selected.assistant_id,
                items=workspace_items,
                assistants=assistants,
                kind="plugins",
            )

st.subheader("Delete assistant")
if selected is None:
    st.caption("请先在上方选择一个现有助手，才能执行删除。")
else:
    st.warning(
        "删除会同时清理助手记录、对应容器、运行时目录和工作空间目录。"
        "这是不可逆操作。"
    )
    with st.form("assistant-delete-form"):
        delete_confirm = st.checkbox("我确认要删除当前助手及其相关资源")
        delete_assistant_id = st.text_input(
            "请输入 Assistant ID 进行二次确认",
            value="",
            help=f"请准确输入：{selected.assistant_id}",
        )
        delete_submitted = st.form_submit_button("Delete assistant")
    if delete_submitted:
        if not delete_confirm:
            st.warning("请先勾选确认删除。")
        elif delete_assistant_id.strip() != selected.assistant_id:
            st.warning("Assistant ID 不匹配，已取消删除。")
        else:
            try:
                deleted = service.delete_assistant(selected.assistant_id)
            except ValueError as exc:
                st.error(f"删除失败：{exc}")
            else:
                if deleted is None:
                    st.warning(f"Assistant '{selected.assistant_id}' not found.")
                else:
                    st.success(f"Assistant '{deleted.assistant_id}' deleted.")
                    st.code(
                        "\n".join(
                            [
                                f"Container: {deleted.container_name}",
                                f"Container removed: {deleted.container_removed}",
                                f"Workspace removed: {deleted.workspace_removed}",
                                f"State removed: {deleted.state_removed}",
                            ]
                        )
                    )
                    st.rerun()

st.subheader("Assistant inventory")
st.dataframe(
    [
        {
            "assistant_id": item.assistant_id,
            "name": item.name,
            "capability_profile": item.capability_profile,
            "primary_model": item.iron_agent_config.primary_model,
            "auth_mode": item.iron_agent_config.auth_mode,
            "provider_id": item.iron_agent_config.provider_id,
            "provider_base_url": item.iron_agent_config.provider_base_url,
            "workspace_root_id": item.workspace_config.root_id,
            "workspace_slug": item.workspace_config.workspace_slug,
            "auto_place_outputs": item.workspace_config.auto_place_outputs,
            "runtime_status": item.runtime.status,
            "container_name": item.runtime.container_name,
            "workspace_root": format_display_path(item.runtime.workspace_root),
            "state_root": format_display_path(item.runtime.state_root),
            "gateway_url": item.runtime.gateway_url,
            "gateway_token": mask_secret(item.runtime.gateway_token),
            "runtime_error": item.runtime.last_error,
            "probe_status": runtime_probes[item.assistant_id].status,
            "probe_container_exists": runtime_probes[item.assistant_id].container_exists,
            "probe_container_state": runtime_probes[item.assistant_id].container_state,
            "probe_health_state": runtime_probes[item.assistant_id].health_state,
            "probe_gateway_url": runtime_probes[item.assistant_id].gateway_url,
            "probe_error": runtime_probes[item.assistant_id].last_error,
            "runtime_in_sync": (
                runtime_probes[item.assistant_id].container_exists
                and runtime_probes[item.assistant_id].status == item.runtime.status
                and runtime_probes[item.assistant_id].container_id == item.runtime.container_id
                and runtime_probes[item.assistant_id].gateway_url == item.runtime.gateway_url
            ),
            "notes": item.notes,
        }
        for item in assistants
    ],
    width="stretch",
)
