import io
import json
import zipfile

from fastapi.testclient import TestClient

from src.main import app
from src.market import routes as market_routes
from src.market.service import MarketService


def build_archive(*, manifest: dict[str, str], extra_files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for path, content in extra_files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_market_routes_are_mounted(tmp_path) -> None:
    original_service = market_routes.service
    market_routes.service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
    )
    client = TestClient(app)
    try:
        response = client.get("/api/v1/market/skills")
        assert response.status_code == 200
        assert response.json() == []
    finally:
        market_routes.service = original_service


def test_market_skill_download_route_returns_archive(tmp_path) -> None:
    original_service = market_routes.service
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
    )
    market_routes.service = service
    client = TestClient(app)
    archive = build_archive(
        manifest={"name": "Office Docs", "version": "1.0.0"},
        extra_files={"SKILL.md": "# Office Docs\n"},
    )
    try:
        record = service.upload_skill(
            filename="office-docs.zip",
            content=archive,
            slug="office-docs",
            display_name="Office Docs",
            owner="IronAgent",
            version="1.0.0",
            tags="office",
        )
        response = client.get(f"/api/v1/market/skills/{record.id}/download")
        assert response.status_code == 200
        assert response.content == archive
        assert "office-docs.zip" in response.headers["content-disposition"]
    finally:
        market_routes.service = original_service


def test_market_plugin_download_route_returns_archive(tmp_path) -> None:
    original_service = market_routes.service
    service = MarketService(
        store_path=tmp_path / "market.json",
        package_root=tmp_path / "packages",
        upload_root=tmp_path / "uploads",
    )
    market_routes.service = service
    client = TestClient(app)
    archive = build_archive(
        manifest={"name": "Office Export Plugin", "version": "1.0.0"},
        extra_files={"plugin.py": "print('plugin')\n"},
    )
    try:
        record = service.upload_plugin(
            filename="office-export-plugin.zip",
            content=archive,
            slug="office-export-plugin",
            display_name="Office Export Plugin",
            owner="IronAgent",
            version="1.0.0",
            tags="office",
        )
        response = client.get(f"/api/v1/market/plugins/{record.id}/download")
        assert response.status_code == 200
        assert response.content == archive
        assert "office-export-plugin.zip" in response.headers["content-disposition"]
    finally:
        market_routes.service = original_service
