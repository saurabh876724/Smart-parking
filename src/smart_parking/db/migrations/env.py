"""Alembic environment: the migration half of the persistence layer.

Two rules make this file worth reading:

* it never contains a database URL. The URL comes from `configs/app.yaml`
  through `core.config.database_url_for`, the same function the running
  pipeline uses, so a migration and the application cannot point at different
  files. `-x url=...` or `SMART_PARKING_DATABASE_URL` override it for a scratch
  database, which is what the tests use.
* `target_metadata` is the ORM's own metadata, so `alembic revision
  --autogenerate` diffs against `db/models.py` rather than a second description
  of the schema that could drift from it.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

from smart_parking.core.config import DatabaseConfig, database_url_for, load_config
from smart_parking.db import models  # noqa: F401  - importing registers the tables
from smart_parking.db.base import Base
from smart_parking.db.session import build_engine, ensure_sqlite_file

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False`: the default would silence the loggers
    # pytest already attached handlers to, so a test that ran a migration would
    # break log capture for the tests after it.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database() -> DatabaseConfig | None:
    """The configured database, or the one-off override the caller passed."""
    override = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get(
        "SMART_PARKING_DATABASE_URL"
    )
    return DatabaseConfig(url=override) if override else None


def url() -> str:
    return database_url_for(load_config(), _database())


def run_migrations_offline() -> None:
    """Emit SQL without a connection (`alembic upgrade head --sql`).

    Autogenerate is impossible here and nothing needs to be: offline mode is a
    way to read the DDL, not a way to build a database.
    """
    context.configure(
        url=url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the engine the application would build.

    `build_engine` rather than `engine_from_config` is the point: the SQLite
    pragmas the schema depends on are applied here too, and the relative path in
    the config is resolved the same way.
    """
    database = _database()
    ensure_sqlite_file(database_url_for(load_config(), database))
    connectable = build_engine(load_config(), database)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER a column in place; batch mode rebuilds the
            # table instead, so a later revision can change a column at all.
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
