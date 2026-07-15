from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[2] / "src/admin_web/static/index.html"


def read_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_workspace_files_default_load_does_not_request_sync_true() -> None:
    index_html = read_index_html()

    assert "/api/v1/admin/workspace-files/summary?sync=true" not in index_html
    assert "/api/v1/admin/workspace-folders/summary?${params.toString()}" in index_html
    assert (
        "/api/v1/admin/assistants/${encodeURIComponent(assistantId)}"
        "/workspace-files/summary?${params.toString()}" in index_html
    )
    assert "void syncWorkspaceFiles({ silent: true, sync: true });" not in index_html
    assert "syncWorkspaceFiles({ ...options, force: true, sync: true })" not in index_html
    assert "void syncWorkspaceFolders({ silent: true });" in index_html


def test_workspace_files_index_status_metadata_is_rendered() -> None:
    index_html = read_index_html()

    assert "workspaceFoldersPagination" in index_html
    assert "workspaceFoldersIndexStatus" in index_html
    assert "workspaceFilesPaginationByAssistant" in index_html
    assert "workspaceFilesIndexStatusByAssistant" in index_html
    assert "workspaceFilesQueryByAssistant" in index_html
    assert "payload.indexStatus || payload.index_status" in index_html
    assert "payload.compat" in index_html
    assert "索引已过期" in index_html
    assert "last_synced_at" in index_html


def test_workspace_files_root_uses_lightweight_folder_summary() -> None:
    index_html = read_index_html()

    assert "/api/v1/admin/workspace-folders/summary" in index_html
    assert "workspaceFolderSummaries" in index_html
    assert "loadWorkspaceAssistantFiles(assistantId" in index_html
    assert "WORKSPACE_FOLDER_PAGE_SIZE = 10" in index_html
    assert "ensureSync: true" in index_html
    assert "state.workspaceFiles = [" in index_html
    assert "changeWorkspaceFoldersPage" in index_html
    assert "changeWorkspaceAssistantFilesPage" in index_html


def test_workspace_assistant_files_use_backend_tree_parent_pagination() -> None:
    index_html = read_index_html()

    assert "workspaceFilesParentByAssistant" in index_html
    assert "state.currentWorkspaceAssistantId === assistantId" in index_html
    assert "params.set('view', 'tree')" in index_html
    assert "params.set('parent', parentPath)" in index_html
    assert "previousParent === parentPath" in index_html
    assert "applyWorkspaceFilesMetadata({ ...payload, assistantId, parentPath })" in index_html
    assert (
        "loadWorkspaceAssistantFiles(assistantId, { silent: true, force: true, page: 1 })"
        in index_html
    )
    assert "Number(file.child_count ?? file.childCount ?? 0)" in index_html


def test_assistants_page_uses_paginated_endpoint() -> None:
    index_html = read_index_html()

    assert "/api/v1/admin/assistants?${query}" in index_html
    assert "assistantPageItems" in index_html
    assert "assistantsPagination" in index_html
    assert "loadAssistantsPage(state.assistantsPagination.page || 1" in index_html
    assert (
        "filter(item => item && !item.deleted && !item.deletedAt && !item.deleted_at)" in index_html
    )
    assert (
        "state.assistants = state.assistants.filter(assistant => assistant.id !== id)" in index_html
    )


def test_assistants_action_column_stays_visible() -> None:
    index_html = read_index_html()

    assert '<table class="min-w-[1280px] text-left border-collapse">' in index_html
    assert "sticky right-0 z-10 bg-gray-50/95" in index_html
    assert "sticky right-0 z-10 bg-white" in index_html


def test_workspace_refresh_creates_single_assistant_sync_job() -> None:
    index_html = read_index_html()

    assert "/api/v1/admin/workspace-sync-jobs" in index_html
    assert "assistant_id: assistantId" in index_html
    assert "mode: 'index_only'" in index_html
    assert "admin-workspace-refresh" in index_html


def test_assistant_gateway_token_is_rendered_in_assistants_list_only() -> None:
    index_html = read_index_html()

    assert "Gateway Token" in index_html
    assert "const gatewayToken = a.gatewayToken || '';" in index_html
    assert "assistant-detail-token" not in index_html
    assert "assistant-detail-copy-token" not in index_html


def test_delete_assistant_clears_workspace_state() -> None:
    index_html = read_index_html()

    assert "function clearWorkspaceStateForAssistant(assistantId)" in index_html
    assert "state.workspaceFolderSummaries = state.workspaceFolderSummaries.filter" in index_html
    assert (
        "state.workspaceFiles = state.workspaceFiles.filter("
        "file => file.assistant_id !== assistantId)" in index_html
    )
    assert "state.workspaceAssistantScanRequestedIds.delete(assistantId)" in index_html
    assert "state.workspaceSyncJobPollingIds.delete(jobId)" in index_html
    assert "clearWorkspaceStateForAssistant(id)" in index_html


def test_workspace_sync_job_polling_refreshes_assistant_files() -> None:
    index_html = read_index_html()

    assert (
        "function scheduleWorkspaceSyncJobRefresh(assistantId, payload = {}, options = {})"
        in index_html
    )
    assert "pollWorkspaceSyncJobUntilDone(assistantId, jobId, options)" in index_html
    assert "/api/v1/admin/workspace-sync-jobs/${encodeURIComponent(jobId)}" in index_html
    assert "loadWorkspaceAssistantFiles(assistantId, {" in index_html
    assert "ensureSync: false" in index_html


def test_admin_view_hash_restores_active_page_after_refresh() -> None:
    index_html = read_index_html()

    assert "const ADMIN_VIEW_NAMES = new Set" in index_html
    assert "viewFromLocationHash()" in index_html
    assert "window.addEventListener('hashchange'" in index_html
    assert "switchView(initialView, { updateHash: false })" in index_html
    assert "updateViewHash(targetView, Boolean(options.replaceHash))" in index_html


def test_workspace_root_refresh_warns_when_index_stays_stale() -> None:
    index_html = read_index_html()

    assert "function rootWorkspaceStaleCount()" in index_html
    assert "Number(status?.stale_count || status?.staleCount || 0)" in index_html
    assert "仍有 ${staleCount} 个工作区索引过期" in index_html
    assert "请打开对应工作区后刷新索引" in index_html


def test_modal_buttons_detach_nested_modal_roots_before_opening() -> None:
    index_html = read_index_html()

    assert index_html.count("const modal = prepareModalRoot('modal-model-form');") == 2
    assert "const modal = prepareModalRoot('modal-ast-plugins');" in index_html
    assert "const modal = prepareModalRoot('modal-add-ast-plugin');" in index_html
    assert "const modal = prepareModalRoot('modal-workspace-upload');" in index_html
    assert index_html.count("const modal = prepareModalRoot('modal-workspace-delete');") == 2


def test_model_form_modal_closes_before_toast_container() -> None:
    index_html = read_index_html()

    modal_start = index_html.index('id="modal-model-form"')
    toast_start = index_html.index("<!-- Toast -->")
    modal_html = index_html[modal_start:toast_start]

    assert 'id="model-apikey"' in modal_html
    assert (
        'id="model-apikey" class="w-full bg-gray-50 border border-gray-200 rounded-xl'
        in modal_html
    )
    assert modal_html.rstrip().endswith("</div>")
