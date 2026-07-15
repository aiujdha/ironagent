from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[2] / "src/admin_web/static/index.html"


def read_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_workspace_items_force_keeps_index_read_semantics() -> None:
    index_html = read_index_html()

    assert "?force=true&_=${Date.now()}" in index_html
    assert "/workspace-items${query}" in index_html
    assert "正在读取已安装能力索引" in index_html
    assert "已读取索引" in index_html
    assert "索引待刷新，请创建刷新任务" in index_html
    assert "refresh_required" in index_html
    assert "refreshRequired" in index_html


def test_workspace_items_force_copy_does_not_claim_workspace_scan() -> None:
    index_html = read_index_html()

    assert "正在从工作空间刷新能力" not in index_html
    assert "已从工作空间刷新能力" not in index_html
    assert "正在同步 工作空间 已安装能力" not in index_html
    assert "同步已安装能力失败" not in index_html
    assert "X-Admin-Token" not in index_html


def test_add_capability_installs_package_for_assistant() -> None:
    index_html = read_index_html()

    assert 'id="add-ast-plugin-file"' in index_html
    assert "installCapabilityPackageToAssistant(ast, packageType, uploadFile)" in index_html
    assert "/workspace-items/install" in index_html
    assert "formData.append('kind', packageType === 'plugin' ? 'plugins' : 'skills')" in index_html
    assert "markAssistantItemInstalled(ast, installedItem, result)" in index_html
    assert "uploadCapabilityPackageToAssistant" not in index_html
    assert "target_bucket', 'temp'" not in index_html
