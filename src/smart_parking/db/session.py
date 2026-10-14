"""Engine and session factory.

Handed a `DatabaseConfig` (or a whole `AppConfig`) - this module never opens
YAML, and it never learns what a project root is: turning a relative sqlite
path into an absolute one is `core.config.database_url_for`'s job, because
path resolution belongs to the loader that owns it.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from smart_parking.core.config import AppConfig, DatabaseConfig, database_url_for


def sqlite_file(url: str) -> Path | None:
    """The file a file-based sqlite URL points at, or None for anything else.

    None covers an in-memory database (`sqlite://`, `sqlite:///:memory:`), a server
    URL (postgres, mysql) and a malformed prefix. The path is only meaningful once
    `core.config.database_url_for` has anchored a relative one to the project root -
    before that, "which database" is a question about the working directory, which
    is the accident this function exists to make visible.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    target = url[len(prefix):].split("?")[0].replace("\\", "/")
    if not target or target.startswith(":memory:"):
        return None
    return Path(target)


def new_sqlite_file(url: str) -> Path | None:
    """The path when `url` names a file database that is not on disk yet.

    None for an existing file, an in-memory database and a server URL. This is the
    question a caller asks *before* the first connection, because SQLite answers a
    name that does not exist by creating it, silently and empty.
    """
    path = sqlite_file(url)
    if path is None or path.exists():
        return None
    return path


def new_database_notice(url: str) -> str | None:
    """The warning to print when `url` points at a file database that is not there.

    None for anything else. Both `scripts/serve.py` and `scripts/migrate.py` print
    it, so the wording a deployment sees once is the wording the other entry point
    uses - and the one thing they have to agree about is that this is a *new* file,
    not the existing one reached by a different route.
    """
    path = new_sqlite_file(url)
    if path is None:
        return None
    return (
        f"{path} does not exist, so this process is creating an empty database there.\n"
        "  If you meant the file you already have, the name it is being given is wrong:\n"
        "  check SMART_PARKING_DATABASE_URL and `database.url` in configs/app.yaml.\n"
        "  The path is resolved against the project root either way, so this is a\n"
        "  different file being named, not the same one reached from elsewhere."
    )


def ensure_sqlite_file(url: str) -> None:
    """Create the directory a file-based sqlite URL points at.

    SQLite will happily open a database in an existing directory and fail
    confusingly in a missing one, and `data/` is gitignored until the first run
    creates it. Only the parent directory is made; the file itself is SQLite's
    to create, so a typo in the filename still shows up as SQLite's own error.
    """
    path = sqlite_file(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def build_engine(cfg: AppConfig, database: DatabaseConfig | None = None) -> Engine:
    """Engine for this configuration, with the pragmas the schema depends on."""
    url = database_url_for(cfg, database)
    db = database or cfg.database
    kwargs: dict = {"echo": db.echo, "future": True}
    path = sqlite_file(url)
    is_file_database = path is not None
    if url.startswith("sqlite"):
        if not is_file_database:
            # One shared in-memory database: without StaticPool every new
            # connection would be a separate, empty database, and a test that
            # writes then reads back would find nothing.
            kwargs["poolclass"] = StaticPool
        # pysqlite refuses to let a connection be used from a thread other than
        # the one that created it, and this project has two threads that touch the
        # database: the persistence worker writes while the calling thread seeds
        # configuration and reads the run's summary. One writer at a time is the
        # design of `services/persistence.py`, and the busy timeout below is what
        # covers the overlap - so what this flag gives up is pysqlite's warning,
        # not a safety property the code does not already hold.
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            # SQLite ignores foreign keys unless told otherwise, so an schema
            # full of REFERENCES would enforce nothing at all. Every migration
            # and every test in this phase depends on this line.
            cursor.execute("PRAGMA foreign_keys=ON")
            # A writer should wait for a lock rather than fail mid-transaction.
            cursor.execute("PRAGMA busy_timeout=5000")
            if is_file_database:
                # Only for a file database: an in-memory one has no journal. WAL
                # is what lets the run's reader (the end-of-run report, and later
                # any API) read while the persistence worker is mid-transaction,
                # instead of waiting on its commit lock - the architecture doc
                # names WAL for the same reason. `synchronous` is deliberately
                # left at SQLite's own default: this database holds visits, and a
                # faster commit that can lose the last transaction is not a trade
                # the specification asks for.
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back and raise on failure.

    The rollback is deliberately not wrapped in a try/except of its own and the
    original error is re-raised. A persistence layer that swallowed a database
    failure would leave the pipeline reporting sessions it never wrote, which
    is the one outcome worse than a crash - and the AI loop must not be the
    place that discovers it.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class Database:
    """Small holder so a caller passes one object instead of an engine, a
    factory and a URL that have to stay consistent with each other."""

    def __init__(self, engine: Engine, session_factory: sessionmaker[Session], url: str) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.url = url
        # The file a *file-based* sqlite URL points at, already anchored to the
        # project root by `database_url_for`. None for an in-memory or server
        # database.
        self.file: Path | None = sqlite_file(url)

    def new_file_notice(self) -> str | None:
        """`new_database_notice` for the URL this database is open on.

        Nothing refuses a brand-new file - a first run and a typo are the same shape
        on disk, and refusing would break the former - so the guard is this line at
        startup rather than an error, and the second database is discovered there
        instead of by an empty dashboard.
        """
        return new_database_notice(self.url)

    @classmethod
    def from_config(cls, cfg: AppConfig, database: DatabaseConfig | None = None) -> "Database":
        url = database_url_for(cfg, database)
        ensure_sqlite_file(url)
        engine = build_engine(cfg, database)
        return cls(engine, make_session_factory(engine), url)

    @contextmanager
    def scope(self) -> Iterator[Session]:
        with session_scope(self.session_factory) as session:
            yield session

    def create_all(self) -> None:
        """Create every table from the models - tests and throwaway databases.

        Production databases are built by Alembic (`scripts/migrate.py`), not
        by this method; keeping the two paths separate is what makes the
        migration worth trusting.
        """
        from smart_parking.db.base import Base

        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        from smart_parking.db.base import Base

        Base.metadata.drop_all(self.engine)

    def check(self, required: tuple[str, ...] = ()) -> dict[str, object]:
        """Is there a database, and has it been migrated - the `/health` question.

        Reflection rather than a hand-written query, so the same answer comes back
        from SQLite and from Postgres. A missing table is reported by name instead
        of raising, because "you have not run the migrations" is a health finding,
        not a crash.
        """
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            names = set(inspect(self.engine).get_table_names())
            missing = [table for table in required if table not in names]
            return {"reachable": True, "dialect": self.engine.dialect.name, "missing_tables": missing}
        except SQLAlchemyError as exc:
            return {
                "reachable": False,
                "dialect": self.engine.dialect.name,
                "error": str(exc),
                "missing_tables": list(required),
            }
