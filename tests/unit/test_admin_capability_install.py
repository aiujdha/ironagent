import hashlib
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.admin.capability_install import AssistantCapabilityInstallService


@pytest.mark.asyncio
async def test_uploaded_package_uses_hash_slug_when_filename_has_no_ascii_slug() -> None:
    calls: list[dict[str, Any]] = []

    class StubMarketService:
        uploaded_slug = ""

        def upload_skill(self, **kwargs: Any) -> SimpleNamespace:
            self.uploaded_slug = str(kwargs["slug"])
            calls.append({"upload": kwargs})
            return SimpleNamespace(
                id="market-1",
                slug=kwargs["slug"],
                name="Manifest Skill",
                version="1.0.0",
            )

        def approve_skill(self, record_id: str) -> SimpleNamespace:
            calls.append({"approve": record_id})
            return SimpleNamespace(
                id=record_id,
                slug=self.uploaded_slug,
                name="Manifest Skill",
                version="1.0.0",
            )

        async def ainstall_skill(self, *, record_id: str, assistant_id: str) -> SimpleNamespace:
            calls.append({"install": (record_id, assistant_id)})
            return SimpleNamespace(
                assistant_id=assistant_id,
                install_path="skills/manifest-skill",
                install_count=1,
                sync_status="synced",
                sync_error="",
                runtime_reload_status="applied",
                runtime_reload_error="",
                action="install",
                previous_version="",
            )

    content = b"archive-bytes"
    expected_slug = f"capability-{hashlib.sha256(content).hexdigest()[:12]}"
    service = AssistantCapabilityInstallService(
        market_service=cast(Any, StubMarketService()),
        workspace_item_index_store=SimpleNamespace(name="index-store"),
    )

    result = await service.ainstall_uploaded_package(
        assistant_id="demo-assistant",
        kind="skill",
        filename="能力包.zip",
        content=content,
    )

    upload_call = calls[0]["upload"]
    assert isinstance(upload_call, dict)
    assert upload_call["slug"] == expected_slug
    assert upload_call["display_name"] == ""
    assert calls[1] == {"approve": "market-1"}
    assert calls[2] == {"install": ("market-1", "demo-assistant")}
    assert result.kind == "skills"
    assert result.slug == expected_slug
    assert result.runtime_reload_status == "applied"
