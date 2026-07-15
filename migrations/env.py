from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text

from src.shared.database import Base, get_database_url
import src.shared.repositories  # noqa: F401
import src.workspace_files.database_models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", get_database_url().replace("%", "%%"))

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except KeyError:
        pass

target_metadata = Base.metadata


def _ensure_mysql_version_table_width(connection) -> None:
    if connection.dialect.name != "mysql":
        return

    inspector = inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        connection.execute(
            text(
                "CREATE TABLE alembic_version ("
                "version_num VARCHAR(191) NOT NULL, "
                "PRIMARY KEY (version_num)"
                ")"
        )
            )
        return

    version_columns = {
        column["name"]: column
        for column in inspector.get_columns("alembic_version")
    }
    version_num = version_columns.get("version_num")
    length = getattr(getattr(version_num, "get", lambda _key, _default=None: None)("type"), "length", None)
    if isinstance(length, int) and length >= 191:
        return
    connection.execute(
        text("ALTER TABLE alembic_version MODIFY version_num VARCHAR(191) NOT NULL")
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _ensure_mysql_version_table_width(connection)
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        if connection.dialect.name == "mysql":
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
