from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_migration() -> Any:
    path = REPO_ROOT / "migrations" / "versions" / "029_workspace_file_long_path_columns.py"
    spec = importlib.util.spec_from_file_location("migration_029_workspace_file_paths", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mysql_upgrade_uses_single_combined_alter(monkeypatch: Any) -> None:
    migration = _load_migration()
    columns = [{"name": name} for name, _, _ in migration._TEXT_COLUMNS]
    bind_execute_sql: list[str] = []
    op_execute_sql: list[str] = []

    class FakeBind:
        dialect = SimpleNamespace(name="mysql")

        def execute(self, statement: Any) -> None:
            bind_execute_sql.append(str(statement))

    class FakeInspector:
        def has_table(self, table_name: str) -> bool:
            return table_name == "workspace_files"

        def get_columns(self, table_name: str) -> list[dict[str, str]]:
            assert table_name == "workspace_files"
            return columns

    class FakeOp:
        @staticmethod
        def get_bind() -> FakeBind:
            return FakeBind()

        @staticmethod
        def execute(statement: Any) -> None:
            op_execute_sql.append(str(statement))

        @staticmethod
        def alter_column(*_: Any, **__: Any) -> None:
            raise AssertionError("MySQL upgrade should use one combined ALTER TABLE")

    monkeypatch.setattr(migration, "inspect", lambda _: FakeInspector())
    monkeypatch.setattr(migration, "op", FakeOp)

    migration.upgrade()

    alter_sql = [sql for sql in op_execute_sql if sql.startswith("ALTER TABLE workspace_files")]
    assert len(alter_sql) == 1
    assert alter_sql[0].count("MODIFY `") == len(migration._TEXT_COLUMNS)
    assert any("UPDATE workspace_files SET object_url = ''" in sql for sql in bind_execute_sql)
