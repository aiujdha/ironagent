import pytest

from src.shared.config import Settings
from src.shared.workspace_roots import (
    get_workspace_root,
    list_workspace_roots,
    normalize_workspace_slug,
)


def test_list_workspace_roots_returns_default_root_when_no_override(tmp_path) -> None:
    settings = Settings(
        assistants_workspace_root=tmp_path / "workspaces",
        default_workspace_root_id="default",
        allowed_workspace_roots_json="",
    )

    roots = list_workspace_roots(settings)

    assert len(roots) == 1
    assert roots[0].id == "default"
    assert roots[0].host_path == tmp_path / "workspaces"


def test_list_workspace_roots_merges_default_and_custom_roots(tmp_path) -> None:
    managed_root = tmp_path / "managed"
    settings = Settings(
        assistants_workspace_root=tmp_path / "workspaces",
        default_workspace_root_id="default",
        allowed_workspace_roots_json=(
            f'[{{"id":"managed","label":"Managed","host_path":"{managed_root}"}}]'
        ),
    )

    roots = list_workspace_roots(settings)

    assert [entry.id for entry in roots] == ["default", "managed"]
    assert get_workspace_root("managed", settings).host_path == managed_root


def test_normalize_workspace_slug_uses_fallback_when_blank() -> None:
    assert normalize_workspace_slug("", fallback="assistant-a") == "assistant-a"


@pytest.mark.parametrize("slug", [".", "..", "a/b", r"a\b"])
def test_normalize_workspace_slug_rejects_invalid_segments(slug: str) -> None:
    with pytest.raises(ValueError):
        normalize_workspace_slug(slug, fallback="assistant-a")
