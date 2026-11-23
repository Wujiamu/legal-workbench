"""Alembic environment; migrations run as the migration role, never the app role."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from legal_workbench.config import load_settings
from legal_workbench.db import Base, sqlalchemy_url
from legal_workbench import models  # noqa: F401  (register metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = load_settings()
if not settings.migration_url:
    raise RuntimeError("LWB_MIGRATION_URL (or LWB_DATABASE_URL) is required for migrations")
# configparser treats '%' specially; escape it.
config.set_main_option("sqlalchemy.url", settings.migration_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.migration_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(sqlalchemy_url(settings.migration_url), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
