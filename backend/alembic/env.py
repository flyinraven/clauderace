"""Alembic environment.

The database URL always comes from the application settings so that migrations
target the same database the app does, whether that is local SQLite or the
SiteGround PostgreSQL instance.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.models import Base  # noqa: E402  (registers every table)

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, and migrations run inside the
    # API process at startup - after every app module has been imported and
    # created its logger. Taking the default switched all of them off for the
    # life of the instance, so nothing the application logged ever reached
    # Render. It was found looking for the reason a description had been
    # discarded and finding no application log lines at all, only alembic's.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render our UTCDateTime decorator as a plain timezone-aware DateTime.

    Without this, autogenerate emits `app.models.base.UTCDateTime()` into the
    migration file but never imports it, so the migration fails with a
    NameError. The underlying column type is identical either way.
    """
    if type_ == "type" and obj.__class__.__name__ == "UTCDateTime":
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
        render_as_batch=settings.is_sqlite,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
            # SQLite cannot ALTER most things; batch mode rebuilds the table.
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
