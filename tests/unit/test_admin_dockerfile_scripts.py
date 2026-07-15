from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_admin_image_copies_only_workspace_file_migration_scripts() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.admin").read_text()

    assert "ARG TARGETOS=linux" in dockerfile
    assert "ARG TARGETARCH=amd64" in dockerfile
    assert "/bin/${TARGETOS}/${TARGETARCH}/kubectl" in dockerfile
    assert "/bin/linux/amd64/kubectl" not in dockerfile
    assert (
        "COPY scripts/migrate_workspace_files_to_mysql.py "
        "/app/scripts/migrate_workspace_files_to_mysql.py"
    ) in dockerfile
    assert (
        "COPY scripts/compare_workspace_files_backends.py "
        "/app/scripts/compare_workspace_files_backends.py"
    ) in dockerfile
    assert "COPY scripts/sql/" not in dockerfile
    assert "COPY scripts /app/scripts" not in dockerfile
    assert "migrate_workspace_files_to_sqlite.py" not in dockerfile
