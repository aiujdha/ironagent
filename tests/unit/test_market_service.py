import io
import json
import zipfile
from pathlib import Path

import pytest

from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.market.service import MarketService
from src.runtime.models import AssistantRuntimeRecord
from src.shared.async_utils import run_sync
from src.shared.config import get_settings
from src.shared.json_store import write_json_object
from src.workspace_files.item_index import WorkspaceItemIndexStore
from src.workspace_files.repository import build_workspace_file_repository


def build_archive(*, manifest: dict[str, str], extra_files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for path, content in extra_files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def build_files_archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def plugin_runtime_files(plugin_id: str = "spreadsheet-exporter") -> dict[str, str]:
    return {
        "ironagent.plugin.json": json.dumps(
            {
                "id": plugin_id,
                "configSchema": {"type": "object"},
            }
        ),
        "index.js": "export default function activate() {}\n",
    }


def build_assistant_service(tmp_path: Path) -> tuple[AssistantService, Path]:
    workspace_root = tmp_path / "workspaces" / "demo-assistant"
    (workspace_root / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "plugins").mkdir(parents=True, exist_ok=True)

    assistant = AssistantRecord(
        assistant_id="demo-assistant",
        name="Demo Assistant",
        capability_profile="onlyread",
        runtime=AssistantRuntimeRecord(workspace_root=str(workspace_root)),
    )
    store_path = tmp_path / "assistants.json"
    write_json_object(
        store_path,
        {"assistants": [assistant.model_dump(mode="json")]},
    )
    return AssistantService(store_path=store_path), workspace_root


def test_market_service_uploads_and_approves_skill(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Local PDF",
            "version": "1.0.0",
            "description": "Offline PDF helper",
            "author": "IronAgent",
        },
        extra_files={
            "SKILL.md": "# Local PDF\n",
            "scripts/run.py": "print('pdf')\n",
        },
    )
    record = service.upload_skill(
        filename="local-pdf.zip",
        content=archive,
        slug="local-pdf",
        display_name="Local PDF",
        owner="IronAgent",
        version="1.0.0",
        tags="pdf,offline",
    )

    assert record.slug == "local-pdf"
    assert Path(record.local_archive_path or "").exists()
    assert service.list_skills()[0].name == "Local PDF"

    approved = service.approve_skill(record.id)
    assert approved.status == "approved"


def test_market_service_can_skip_cos_sync_for_fast_reads(tmp_path: Path, monkeypatch) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )
    write_json_object(
        tmp_path / "market.json",
        {
            "skills": [
                {
                    "id": "skill-local",
                    "name": "Local Skill",
                    "slug": "local-skill",
                    "version": "1.0.0",
                    "status": "approved",
                }
            ],
            "plugins": [],
        },
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("COS sync should be skipped")

    monkeypatch.setattr(service, "_discover_cos_records", fail_if_called)

    records = service.list_skills(sync_cos=False)

    assert len(records) == 1
    assert records[0].slug == "local-skill"


def test_market_service_installs_skill_into_assistant_workspace(tmp_path: Path) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    item_index_store = WorkspaceItemIndexStore(tmp_path / "workspace-item-index.db")
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
        workspace_item_index_store=item_index_store,
    )

    archive = build_archive(
        manifest={
            "name": "Office Docs",
            "version": "2.0.0",
            "description": "Document helpers",
            "author": "IronAgent",
        },
        extra_files={
            "SKILL.md": "# Office Docs\n",
            "scripts/convert.py": "print('ok')\n",
        },
    )
    record = service.upload_skill(
        filename="office-docs.zip",
        content=archive,
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="2.0.0",
        tags="office",
    )

    result = service.install_skill(record.id, "demo-assistant")

    installed_root = workspace_root / "skills" / "office-docs"
    assert result.assistant_id == "demo-assistant"
    assert result.install_count == 1
    assert (installed_root / "SKILL.md").exists()
    assert json.loads((installed_root / "manifest.json").read_text(encoding="utf-8"))["name"] == (
        "Office Docs"
    )
    synced = run_sync(
        build_workspace_file_repository().alist_files(
            assistant_id="demo-assistant",
            bucket="skills",
    )
        )
    synced_by_name = {item.filename: item for item in synced}
    assert {
        "office-docs/SKILL.md",
        "office-docs/manifest.json",
        "office-docs/scripts/convert.py",
    }.issubset(synced_by_name)
    assert synced_by_name["office-docs/SKILL.md"].source_type == "capability_package"
    assert synced_by_name["office-docs/SKILL.md"].sync_status == "synced_to_workspace"
    indexed = item_index_store.get_item(
        assistant_id="demo-assistant",
        kind="skills",
        slug="office-docs",
    )
    assert indexed is not None
    assert indexed.name == "Office Docs"
    assert indexed.version == "2.0.0"
    assert indexed.install_path == str(installed_root)
    assert indexed.files_count == 3
    assert indexed.sync_status == "synced"
    assert indexed.runtime_reload_status == "skipped"


def test_market_service_preserves_versions_and_old_version_overwrites(
    tmp_path: Path,
) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    v1 = service.upload_skill(
        filename="office-docs-v1.zip",
        content=build_archive(
            manifest={"name": "Office Docs", "version": "1.0.0"},
            extra_files={"SKILL.md": "# Office Docs v1\n"},
        ),
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="1.0.0",
        tags="office",
    )
    v2 = service.upload_skill(
        filename="office-docs-v2.zip",
        content=build_archive(
            manifest={"name": "Office Docs", "version": "2.0.0"},
            extra_files={"SKILL.md": "# Office Docs v2\n"},
        ),
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="2.0.0",
        tags="office",
    )

    assert [record.version for record in service.list_skills()] == ["1.0.0", "2.0.0"]

    first_install = service.install_skill(v2.id, "demo-assistant")
    old_version_install = service.install_skill(v1.id, "demo-assistant")

    manifest = json.loads(
        (workspace_root / "skills" / "office-docs" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "1.0.0"
    assert first_install.action == "install"
    assert old_version_install.action == "upgrade"
    assert old_version_install.previous_version == "2.0.0"


def test_market_service_does_not_auto_upgrade_installed_fixed_version(
    tmp_path: Path,
) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    v1 = service.upload_plugin(
        filename="spreadsheet-v1.zip",
        content=build_archive(
            manifest={"name": "Spreadsheet Exporter", "version": "1.0.0"},
            extra_files={
                **plugin_runtime_files(),
                "plugin.py": "VERSION = '1.0.0'\n",
            },
        ),
        slug="spreadsheet-exporter",
        display_name="Spreadsheet Exporter",
        owner="IronAgent",
        version="1.0.0",
        tags="excel",
    )
    service.install_plugin(v1.id, "demo-assistant")
    service.upload_plugin(
        filename="spreadsheet-v2.zip",
        content=build_archive(
            manifest={"name": "Spreadsheet Exporter", "version": "2.0.0"},
            extra_files={
                **plugin_runtime_files(),
                "plugin.py": "VERSION = '2.0.0'\n",
            },
        ),
        slug="spreadsheet-exporter",
        display_name="Spreadsheet Exporter",
        owner="IronAgent",
        version="2.0.0",
        tags="excel",
    )

    manifest = json.loads(
        (workspace_root / "plugins" / "spreadsheet-exporter" / "manifest.json").read_text(
            encoding="utf-8"
    )
        )
    assert manifest["version"] == "1.0.0"
    assert [record.version for record in service.list_plugins()] == ["1.0.0", "2.0.0"]


def test_market_service_uploads_plugin_metadata_from_iron_agent_manifest(
    tmp_path: Path,
) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    record = service.upload_plugin(
        filename="ironagent-exporter.zip",
        content=build_files_archive(
            {
                "ironagent.plugin.json": json.dumps(
                    {
                        "id": "com.example.ironagent-exporter",
                        "version": "3.1.0",
                        "description": "IronAgent-native exporter",
                        "author": "IronAgent",
                    }
                ),
                "index.js": "export default function activate() {}\n",
            }
        ),
        slug="ironagent-exporter",
        display_name="",
        owner="",
        version="",
        tags="export",
    )

    assert record.name == "com.example.ironagent-exporter"
    assert record.version == "3.1.0"
    assert record.description == "IronAgent-native exporter"
    assert record.author == "IronAgent"
    assert record.manifest["id"] == "com.example.ironagent-exporter"


def test_market_service_only_syncs_installed_package_scope(tmp_path: Path) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Office Docs",
            "version": "2.0.0",
        },
        extra_files={
            "SKILL.md": "# Office Docs\n",
        },
    )
    record = service.upload_skill(
        filename="office-docs.zip",
        content=archive,
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="2.0.0",
        tags="office",
    )

    calls: list[tuple[str, list[tuple[str, str]]]] = []

    class StubWorkspaceSyncService:
        async def async_sync_assistant_workspace_scopes(self, assistant_id: str, scopes):
            calls.append((assistant_id, list(scopes)))
            return None

    service.workspace_sync_service = StubWorkspaceSyncService()

    result = service.install_skill(record.id, "demo-assistant")

    assert result.install_path == "/workspace/skills/office-docs"
    assert calls == [("demo-assistant", [("skills", "office-docs")])]


def test_market_service_notifies_running_runtime_after_skill_install(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces" / "demo-assistant"
    (workspace_root / "skills").mkdir(parents=True, exist_ok=True)
    assistant = AssistantRecord(
        assistant_id="demo-assistant",
        name="Demo Assistant",
        capability_profile="onlyread",
        runtime=AssistantRuntimeRecord(
            container_name="ironagent-assistant-demo",
            workspace_root=str(workspace_root),
        ),
    )
    store_path = tmp_path / "assistants.json"
    write_json_object(
        store_path,
        {"assistants": [assistant.model_dump(mode="json")]},
    )
    notify_calls: list[tuple[str, str, str | None]] = []

    class FakeProvisioner:
        def notify_workspace_skill_changed(
            self,
            *,
            assistant_id: str,
            skill_slug: str,
            container_name: str | None = None,
        ) -> bool:
            notify_calls.append((assistant_id, skill_slug, container_name))
            return True

    assistant_service = AssistantService(
        store_path=store_path,
        provisioner=FakeProvisioner(),
    )
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )
    archive = build_archive(
        manifest={
            "name": "Office Docs",
            "version": "2.0.0",
        },
        extra_files={
            "SKILL.md": "# Office Docs\n",
        },
    )
    record = service.upload_skill(
        filename="office-docs.zip",
        content=archive,
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="2.0.0",
        tags="office",
    )

    service.install_skill(record.id, "demo-assistant")

    assert notify_calls == [("demo-assistant", "office-docs", "ironagent-assistant-demo")]


def test_market_service_triggers_runtime_reload_after_plugin_install(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces" / "demo-assistant"
    (workspace_root / "plugins").mkdir(parents=True, exist_ok=True)
    assistant = AssistantRecord(
        assistant_id="demo-assistant",
        name="Demo Assistant",
        capability_profile="onlyread",
        runtime=AssistantRuntimeRecord(
            container_name="ironagent-assistant-demo",
            workspace_root=str(workspace_root),
        ),
    )
    store_path = tmp_path / "assistants.json"
    write_json_object(
        store_path,
        {"assistants": [assistant.model_dump(mode="json")]},
    )
    reload_calls: list[tuple[str, str, str, str | None]] = []

    class FakeProvisioner:
        def reload_workspace_capability(
            self,
            *,
            assistant_id: str,
            kind: str,
            slug: str,
            container_name: str | None = None,
        ) -> bool:
            reload_calls.append((assistant_id, kind, slug, container_name))
            return True

    assistant_service = AssistantService(
        store_path=store_path,
        provisioner=FakeProvisioner(),
    )
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )
    record = service.upload_plugin(
        filename="spreadsheet.zip",
        content=build_files_archive(
            {
                "spreadsheet-exporter/ironagent.plugin.json": json.dumps(
                    {
                        "id": "spreadsheet-exporter",
                        "version": "1.0.0",
                        "description": "Exports sheets",
                        "author": "IronAgent",
                    }
                ),
                "spreadsheet-exporter/index.js": "export default function activate() {}\n",
                "spreadsheet-exporter/plugin.py": "print('ok')\n",
            }
        ),
        slug="spreadsheet-exporter",
        display_name="",
        owner="",
        version="",
        tags="excel",
    )

    result = service.install_plugin(record.id, "demo-assistant")

    assert record.name == "spreadsheet-exporter"
    assert record.version == "1.0.0"
    assert (
        workspace_root / "plugins" / "spreadsheet-exporter" / "ironagent.plugin.json"
    ).exists()
    assert (workspace_root / "plugins" / "spreadsheet-exporter" / "plugin.py").exists()
    assert result.runtime_reload_status == "applied"
    assert reload_calls == [
        (
            "demo-assistant",
            "plugins",
            "spreadsheet-exporter",
            "ironagent-assistant-demo",
        )
    ]


def test_market_service_reports_runtime_notify_warning_after_skill_install(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces" / "demo-assistant"
    (workspace_root / "skills").mkdir(parents=True, exist_ok=True)
    assistant = AssistantRecord(
        assistant_id="demo-assistant",
        name="Demo Assistant",
        capability_profile="onlyread",
        runtime=AssistantRuntimeRecord(
            container_name="ironagent-assistant-demo",
            workspace_root=str(workspace_root),
        ),
    )
    store_path = tmp_path / "assistants.json"
    write_json_object(
        store_path,
        {"assistants": [assistant.model_dump(mode="json")]},
    )

    class FakeProvisioner:
        def notify_workspace_skill_changed(
            self,
            *,
            assistant_id: str,
            skill_slug: str,
            container_name: str | None = None,
        ) -> bool:
            return False

    assistant_service = AssistantService(
        store_path=store_path,
        provisioner=FakeProvisioner(),
    )
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )
    archive = build_archive(
        manifest={
            "name": "Office Docs",
            "version": "2.0.0",
        },
        extra_files={
            "SKILL.md": "# Office Docs\n",
        },
    )
    record = service.upload_skill(
        filename="office-docs.zip",
        content=archive,
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="2.0.0",
        tags="office",
    )

    result = service.install_skill(record.id, "demo-assistant")

    assert (workspace_root / "skills" / "office-docs" / "SKILL.md").exists()
    assert result.sync_status == "failed"
    assert result.runtime_reload_status == "failed"
    assert "runtime reload failed" in result.sync_error
    assert "runtime command failed" in result.sync_error


def test_market_service_reports_sync_warning_after_successful_install(tmp_path: Path) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Office Docs",
            "version": "2.0.0",
        },
        extra_files={
            "SKILL.md": "# Office Docs\n",
        },
    )
    record = service.upload_skill(
        filename="office-docs.zip",
        content=archive,
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="2.0.0",
        tags="office",
    )

    class FailingWorkspaceSyncService:
        async def async_sync_assistant_workspace_scopes(self, assistant_id: str, scopes):
            raise RuntimeError("COS is temporarily unavailable")

    service.workspace_sync_service = FailingWorkspaceSyncService()

    result = service.install_skill(record.id, "demo-assistant")

    installed_root = workspace_root / "skills" / "office-docs"
    assert (installed_root / "SKILL.md").exists()
    assert result.install_count == 1
    assert result.sync_status == "failed"
    assert "COS is temporarily unavailable" in result.sync_error


def test_market_service_does_not_leave_workspace_item_when_archive_missing(
    tmp_path: Path,
) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Office Docs",
            "version": "2.0.0",
        },
        extra_files={
            "SKILL.md": "# Office Docs\n",
        },
    )
    record = service.upload_skill(
        filename="office-docs.zip",
        content=archive,
        slug="office-docs",
        display_name="Office Docs",
        owner="IronAgent",
        version="2.0.0",
        tags="office",
    )
    Path(record.local_archive_path or "").unlink()

    with pytest.raises(ValueError, match="package archive not found"):
        service.install_skill(record.id, "demo-assistant")

    assert not (workspace_root / "skills" / "office-docs").exists()


def test_market_service_installs_skill_after_package_root_migration(tmp_path: Path) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    store_path = tmp_path / "market.json"
    old_package_root = tmp_path / "old-root" / "packages"
    new_package_root = tmp_path / "new-root" / "packages"
    service = MarketService(
        store_path=store_path,
        package_root=old_package_root,
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Migrated Skill",
            "version": "1.0.0",
        },
        extra_files={"SKILL.md": "# Migrated Skill\n"},
    )
    record = service.upload_skill(
        filename="migrated-skill.zip",
        content=archive,
        slug="migrated-skill",
        display_name="Migrated Skill",
        owner="IronAgent",
        version="1.0.0",
        tags="migration",
    )
    old_archive = Path(record.local_archive_path or "")
    migrated_archive = new_package_root / "skills" / record.id / old_archive.name
    migrated_archive.parent.mkdir(parents=True, exist_ok=True)
    migrated_archive.write_bytes(old_archive.read_bytes())
    old_archive.unlink()

    migrated_service = MarketService(
        store_path=store_path,
        package_root=new_package_root,
        upload_root=tmp_path / "new-uploads",
        assistant_service=assistant_service,
    )

    result = migrated_service.install_skill(record.id, "demo-assistant")

    assert (workspace_root / "skills" / "migrated-skill" / "SKILL.md").exists()
    assert result.install_count == 1


def test_market_service_installs_skill_from_nested_package_root(tmp_path: Path) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_files_archive(
        {
            "export/nested-skill/manifest.json": json.dumps(
                {
                    "name": "Nested Skill",
                    "version": "1.0.0",
                }
            ),
            "export/nested-skill/SKILL.md": "# Nested Skill\n",
            "export/nested-skill/scripts/run.py": "print('ok')\n",
        }
    )
    record = service.upload_skill(
        filename="nested-skill.zip",
        content=archive,
        slug="nested-skill",
        display_name="Nested Skill",
        owner="IronAgent",
        version="1.0.0",
        tags="nested",
    )

    result = service.install_skill(record.id, "demo-assistant")

    installed_root = workspace_root / "skills" / "nested-skill"
    assert result.install_path == "/workspace/skills/nested-skill"
    assert (installed_root / "SKILL.md").exists()
    assert (installed_root / "manifest.json").exists()
    assert (installed_root / "scripts" / "run.py").exists()
    assert not (installed_root / "export").exists()


def test_market_service_resolves_download_after_package_root_migration(
    tmp_path: Path,
) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )
    migrated_archive = (
        tmp_path / "packages" / "plugins" / "plugin-old" / "legacy-plugin.zip"
    )
    migrated_archive.parent.mkdir(parents=True, exist_ok=True)
    migrated_archive.write_bytes(b"plugin bytes")
    write_json_object(
        tmp_path / "market.json",
        {
            "skills": [],
            "plugins": [
                {
                    "id": "plugin-old",
                    "name": "Legacy Plugin",
                    "slug": "legacy-plugin",
                    "version": "1.0.0",
                    "package_path": (
                        "/app/ironagent/ironagent-feat-hooks/data/market/"
                        "packages/plugins/plugin-old/legacy-plugin.zip"
                    ),
                    "local_archive_path": (
                        "/app/ironagent/ironagent-feat-hooks/data/market/"
                        "packages/plugins/plugin-old/legacy-plugin.zip"
                    ),
                    "storage_backend": "local",
                }
            ],
        },
    )

    target = service.get_plugin_download_target("plugin-old")

    assert target.source == "local"
    assert target.path == str(migrated_archive)
    assert target.filename == "legacy-plugin.zip"


def test_market_service_installs_skill_using_resolved_workspace_when_runtime_path_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    assistant = assistant_service.get_assistant("demo-assistant")
    assert assistant is not None
    assistant.runtime.workspace_root = (
        "/home/oldjohn/project/ironagent/data/workspaces/demo-assistant"
    )
    write_json_object(
        tmp_path / "assistants.json",
        {"assistants": [assistant.model_dump(mode="json")]},
    )
    monkeypatch.setattr(
        "src.assistants.service.get_settings",
        lambda: get_settings().model_copy(
            update={"assistants_workspace_root": tmp_path / "workspaces"}
        ),
    )
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=AssistantService(store_path=tmp_path / "assistants.json"),
    )

    archive = build_archive(
        manifest={
            "name": "Recovered Install",
            "version": "1.0.0",
        },
        extra_files={"SKILL.md": "# Recovered Install\n"},
    )
    record = service.upload_skill(
        filename="recovered-install.zip",
        content=archive,
        slug="recovered-install",
        display_name="Recovered Install",
        owner="IronAgent",
        version="1.0.0",
        tags="recovery",
    )

    result = service.install_skill(record.id, "demo-assistant")

    assert result.install_path == "/workspace/skills/recovered-install"
    assert (workspace_root / "skills" / "recovered-install" / "SKILL.md").exists()


def test_market_service_installs_plugin_into_assistant_workspace(tmp_path: Path) -> None:
    assistant_service, workspace_root = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Spreadsheet Exporter",
            "version": "1.2.0",
            "description": "Exports tables",
            "author": "IronAgent",
        },
        extra_files={
            **plugin_runtime_files(),
            "plugin.py": "print('plugin')\n",
        },
    )
    record = service.upload_plugin(
        filename="spreadsheet-exporter.zip",
        content=archive,
        slug="spreadsheet-exporter",
        display_name="Spreadsheet Exporter",
        owner="IronAgent",
        version="1.2.0",
        tags="excel",
    )

    result = service.install_plugin(record.id, "demo-assistant")

    installed_root = workspace_root / "plugins" / "spreadsheet-exporter"
    assert result.assistant_id == "demo-assistant"
    assert result.install_count == 1
    assert (installed_root / "plugin.py").exists()
    manifest = json.loads((installed_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.0"


def test_market_service_rejects_plugin_without_iron_agent_manifest(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    record = service.upload_plugin(
        filename="legacy-plugin.zip",
        content=build_archive(
            manifest={"name": "Legacy Plugin", "version": "1.0.0"},
            extra_files={"plugin.py": "print('legacy')\n"},
        ),
        slug="legacy-plugin",
        display_name="Legacy Plugin",
        owner="IronAgent",
        version="1.0.0",
        tags="legacy",
    )

    with pytest.raises(ValueError, match="ironagent.plugin.json not found"):
        service.install_plugin(record.id, "demo-assistant")


def test_market_service_resolves_skill_download_target(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Office Docs Download",
            "version": "1.0.0",
        },
        extra_files={"SKILL.md": "# Office Docs Download\n"},
    )
    record = service.upload_skill(
        filename="office-docs-download.zip",
        content=archive,
        slug="office-docs-download",
        display_name="Office Docs Download",
        owner="IronAgent",
        version="1.0.0",
        tags="office",
    )

    target = service.get_skill_download_target(record.id)

    assert target.source == "local"
    assert target.filename == "office-docs-download.zip"
    assert Path(target.path).read_bytes() == archive


def test_market_service_resolves_plugin_download_target(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    archive = build_archive(
        manifest={
            "name": "Office Plugin Download",
            "version": "1.0.0",
        },
        extra_files={
            **plugin_runtime_files("office-plugin-download"),
            "plugin.py": "print('plugin download')\n",
        },
    )
    record = service.upload_plugin(
        filename="office-plugin-download.zip",
        content=archive,
        slug="office-plugin-download",
        display_name="Office Plugin Download",
        owner="IronAgent",
        version="1.0.0",
        tags="office",
    )

    target = service.get_plugin_download_target(record.id)

    assert target.source == "local"
    assert target.filename == "office-plugin-download.zip"
    assert Path(target.path).read_bytes() == archive


def test_market_service_prefers_local_archive_over_cos_download_target(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    local_archive = tmp_path / "packages" / "skills" / "skill-123" / "package.zip"
    local_archive.parent.mkdir(parents=True, exist_ok=True)
    local_archive.write_bytes(b"local copy")
    write_json_object(
        tmp_path / "market.json",
        {
            "skills": [
                {
                    "id": "skill-123",
                    "name": "COS Skill",
                    "slug": "cos-skill",
                    "version": "1.0.0",
                    "package_path": (
                        "https://bucket.cos.ap-shanghai.myqcloud.com/"
                        "team-a/skills/skill-123/package.zip"
                    ),
                    "local_archive_path": str(local_archive),
                    "storage_backend": "cos",
                }
            ],
            "plugins": [],
        },
    )

    target = service.get_skill_download_target("skill-123")

    assert target.source == "local"
    assert target.path == str(local_archive)
    assert target.filename == "package.zip"


def test_market_service_strips_query_string_from_remote_download_filename(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    write_json_object(
        tmp_path / "market.json",
        {
            "skills": [
                {
                    "id": "skill-query",
                    "name": "Signed COS Skill",
                    "slug": "signed-cos-skill",
                    "version": "1.0.0",
                    "package_path": (
                        "https://bucket.cos.ap-shanghai.myqcloud.com/"
                        "team-a/skills/skill-query/package.zip?q-sign-algorithm=sha1&q-ak=abc"
                    ),
                    "storage_backend": "cos",
                }
            ],
            "plugins": [],
        },
    )

    from src.market.service import download_file_from_cos, is_cos_enabled

    original_download = download_file_from_cos
    original_enabled = is_cos_enabled
    try:
        import src.market.service as market_service_module

        market_service_module.download_file_from_cos = lambda key: (True, b"cos-bytes")
        market_service_module.is_cos_enabled = lambda: True

        target = service.get_skill_download_target("skill-query")

        assert target.source == "local"
        assert target.cleanup is True
        assert target.filename == "package.zip"
        assert Path(target.path).read_bytes() == b"cos-bytes"
    finally:
        import src.market.service as market_service_module

        market_service_module.download_file_from_cos = original_download
        market_service_module.is_cos_enabled = original_enabled


def test_market_service_materializes_private_skill_download_from_cos_url(
    tmp_path: Path,
) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    write_json_object(
        tmp_path / "market.json",
        {
            "skills": [
                {
                    "id": "skill-private",
                    "name": "Private Skill",
                    "slug": "private-skill",
                    "version": "1.0.0",
                    "package_path": (
                        "https://bucket.cos.ap-shanghai.myqcloud.com/"
                        "heinu/skills/skill-private/private-skill.zip"
                    ),
                    "local_archive_path": str(tmp_path / "missing.zip"),
                    "storage_backend": "cos",
                }
            ],
            "plugins": [],
        },
    )

    from src.market.service import download_file_from_cos_url, is_cos_enabled

    original_download_url = download_file_from_cos_url
    original_enabled = is_cos_enabled
    try:
        import src.market.service as market_service_module

        market_service_module.download_file_from_cos_url = lambda url: (True, b"private-bytes")
        market_service_module.is_cos_enabled = lambda: False

        target = service.get_skill_download_target("skill-private")

        assert target.source == "local"
        assert target.cleanup is True
        assert target.filename == "private-skill.zip"
        assert Path(target.path).read_bytes() == b"private-bytes"
    finally:
        import src.market.service as market_service_module

        market_service_module.download_file_from_cos_url = original_download_url
        market_service_module.is_cos_enabled = original_enabled


def test_market_service_materializes_private_plugin_download_from_cos(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    write_json_object(
        tmp_path / "market.json",
        {
            "skills": [],
            "plugins": [
                {
                    "id": "plugin-private",
                    "name": "Private Plugin",
                    "slug": "private-plugin",
                    "version": "1.0.0",
                    "package_path": (
                        "https://bucket.cos.ap-shanghai.myqcloud.com/"
                        "plugins/plugin-private/private-plugin.zip"
                    ),
                    "storage_backend": "cos",
                }
            ],
        },
    )

    from src.market.service import download_file_from_cos, is_cos_enabled

    original_download = download_file_from_cos
    original_enabled = is_cos_enabled
    try:
        import src.market.service as market_service_module

        market_service_module.download_file_from_cos = lambda key: (True, b"plugin-bytes")
        market_service_module.is_cos_enabled = lambda: True

        target = service.get_plugin_download_target("plugin-private")

        assert target.source == "local"
        assert target.cleanup is True
        assert target.filename == "private-plugin.zip"
        assert Path(target.path).read_bytes() == b"plugin-bytes"
    finally:
        import src.market.service as market_service_module

        market_service_module.download_file_from_cos = original_download
        market_service_module.is_cos_enabled = original_enabled


def test_market_service_extracts_cos_object_key_without_dropping_prefix(tmp_path: Path) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    key = service._object_key_from_path(
        "https://example.invalid/team-a/skills/skill-123/package.zip"
    )

    assert key == "team-a/skills/skill-123/package.zip"


def test_market_service_strips_configured_cos_prefix_from_endpoint_urls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )

    monkeypatch.setattr(
        "src.market.common.cos_client._cos_settings",
        lambda: {
            "secret_id": "",
            "secret_key": "",
            "session_token": "",
            "bucket": "bucket",
            "region": "ap-shanghai",
            "endpoint": "",
            "prefix": "team-a",
        },
    )

    key = service._object_key_from_path(
        "https://bucket.cos.ap-shanghai.myqcloud.com/team-a/skills/skill-123/package.zip"
    )

    assert key == "skills/skill-123/package.zip"


def test_market_service_deletes_cos_object_without_double_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    assistant_service, _ = build_assistant_service(tmp_path)
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
        assistant_service=assistant_service,
    )
    write_json_object(
        tmp_path / "market.json",
        {
            "skills": [
                {
                    "id": "skill-123",
                    "name": "Prefixed Skill",
                    "slug": "prefixed-skill",
                    "version": "1.0.0",
                    "package_path": (
                        "https://bucket.cos.ap-shanghai.myqcloud.com/"
                        "team-a/skills/skill-123/package.zip"
                    ),
                    "storage_backend": "cos",
                }
            ],
            "plugins": [],
        },
    )
    deleted_keys: list[str] = []

    monkeypatch.setattr(
        "src.market.common.cos_client._cos_settings",
        lambda: {
            "secret_id": "",
            "secret_key": "",
            "session_token": "",
            "bucket": "bucket",
            "region": "ap-shanghai",
            "endpoint": "",
            "prefix": "team-a",
        },
    )
    monkeypatch.setattr(
        "src.market.service.delete_file_from_cos",
        lambda key: deleted_keys.append(key) or (True, "Deleted"),
    )

    service.delete_skill("skill-123")

    assert deleted_keys == ["skills/skill-123/package.zip"]
