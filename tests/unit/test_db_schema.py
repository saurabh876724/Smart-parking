"""Phase 7 schema: the config URL, the ORM models and the Alembic migration.

Three things are checked here, and each is a rule the architecture doc states:

* the database URL comes from the config, and a relative sqlite path resolves
  from the project root - never from whatever directory the process happened to
  start in;
* `alembic upgrade head` builds a schema that is **the same schema** the models
  describe, compared column by column, index by index, constraint by constraint,
  because a migration nobody diffs against the models is a second description of
  the database waiting to drift;
* `downgrade` reverses it.

The constraint tests use savepoints (`session.begin_nested`), because a rejected
write poisons the transaction it was attempted in: without the savepoint the
fixture would roll back the rows the test set up to be violated.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from re import compile as compile_pattern

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from smart_parking.core.config import AppConfig, DatabaseConfig, database_url_for, load_config
from smart_parking.core.schemas import Role, SessionStatus
from smart_parking.db.base import Base
from smart_parking.db.models import AUDIT_ACTIONS, ParkingSession, User
from smart_parking.db.repositories import Repositories

ROOT = Path(__file__).resolve().parents[2]
TABLES = {
    "cameras",
    "parking_slots",
    "license_plates",
    "vehicles",
    "parking_sessions",
    "slot_occupancy_history",
    "detection_logs",
    # Phase 8. Listed here as well as in `db/models.py :: MIGRATED_TABLES` on
    # purpose: this set is what the two tests below diff **migration against
    # models**, so a table has to be named to be compared at all - and adding a
    # model without its migration is exactly the mistake a smaller list hides.
    "users",
    "audit_logs",
}


def at(seconds: float) -> datetime:
    """A stored datetime, offset from one fixed clock. Naive UTC, like every
    column here, and fixed so two calls with the same offset compare equal."""
    return datetime(2026, 1, 12, 9, 0, 0) + timedelta(seconds=seconds)


# -- the URL ----------------------------------------------------------------
def test_relative_sqlite_path_resolves_from_the_project_root(tmp_path):
    cfg = AppConfig(project_root=tmp_path)
    url = database_url_for(cfg, DatabaseConfig(url="sqlite:///data/smart_parking.db"))
    assert url == f"sqlite:///{(tmp_path / 'data' / 'smart_parking.db').as_posix()}"


def test_the_configured_default_is_the_documented_one():
    assert load_config(root=ROOT).database.url == "sqlite:///data/smart_parking.db"


def test_memory_absolute_and_other_dialect_urls_pass_through():
    cfg = AppConfig(project_root=Path("/project"))
    assert database_url_for(cfg, DatabaseConfig(url="sqlite://")) == "sqlite://"
    assert database_url_for(cfg, DatabaseConfig(url="sqlite:///:memory:")) == "sqlite:///:memory:"
    assert (
        database_url_for(cfg, DatabaseConfig(url="sqlite:////var/lib/parking.db"))
        == "sqlite:////var/lib/parking.db"
    )
    assert (
        database_url_for(cfg, DatabaseConfig(url="postgresql+psycopg://u:p@host/db"))
        == "postgresql+psycopg://u:p@host/db"
    )


def test_a_query_string_survives_resolution(tmp_path):
    cfg = AppConfig(project_root=tmp_path)
    assert database_url_for(cfg, DatabaseConfig(url="sqlite:///data/x.db?mode=ro")).endswith(
        "/data/x.db?mode=ro"
    )


@pytest.mark.parametrize("url", ["", "sqllite:///typo.db", "data/smart_parking.db"])
def test_a_url_that_names_no_database_dialect_is_refused(url):
    with pytest.raises(ValueError):
        DatabaseConfig(url=url)


def test_the_environment_variable_names_the_database_when_no_argument_does(tmp_path, monkeypatch):
    """One variable name for the running API and the migrations.

    A deployment sets SMART_PARKING_DATABASE_URL and both halves of the
    persistence layer follow it; the YAML value is the laptop default, not a
    second opinion. An explicit argument still wins, because a caller naming
    its own database (a test's scratch file) is more specific than either.
    """
    cfg = AppConfig(project_root=tmp_path, database=DatabaseConfig(url="sqlite:///data/yaml.db"))
    monkeypatch.setenv("SMART_PARKING_DATABASE_URL", "sqlite:///data/env.db")
    assert database_url_for(cfg).endswith("/data/env.db")
    assert database_url_for(cfg, DatabaseConfig(url="sqlite:///data/arg.db")).endswith(
        "/data/arg.db"
    )
    monkeypatch.delenv("SMART_PARKING_DATABASE_URL")
    assert database_url_for(cfg).endswith("/data/yaml.db")


def test_a_nonsense_environment_variable_is_refused_like_any_other_url(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_PARKING_DATABASE_URL", "sqllite:///typo.db")
    with pytest.raises(ValueError):
        database_url_for(AppConfig(project_root=tmp_path))


# -- the file behind the URL -------------------------------------------------
def test_the_same_file_from_any_working_directory(tmp_path, monkeypatch):
    """The accident this prevents by name: a second, empty database.

    SQLite answers a relative path against the *working directory*, so the same
    configuration run from `deploy/` opens `deploy/data/smart_parking.db` and, on a
    machine where that directory exists, **creates the file**. Nothing errors; the
    dashboard just has no accounts, no cameras and no visits, because it is reading a
    database nobody wrote to. Anchoring to `AppConfig.project_root` is what makes the
    answer the same from either directory.
    """
    cfg = AppConfig(project_root=tmp_path)
    here = database_url_for(cfg)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert database_url_for(cfg) == here
    assert not (elsewhere / "data").exists(), "a second database directory was created"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("sqlite:////var/lib/parking.db", Path("/var/lib/parking.db")),
        ("sqlite:///", None),
        ("sqlite:///:memory:", None),
        ("sqlite:///data/smart_parking.db?mode=ro", Path("data/smart_parking.db")),
        ("postgresql+psycopg://u:p@host/db", None),
    ],
)
def test_the_file_a_url_points_at_and_none_for_what_has_no_file(url, expected):
    from smart_parking.db.session import sqlite_file

    assert sqlite_file(url) == expected


def test_a_file_that_is_not_there_yet_is_said_before_it_is_made(tmp_path):
    from smart_parking.db.session import new_database_notice, new_sqlite_file

    url = f"sqlite:///{(tmp_path / 'smart_parking.db').as_posix()}"
    missing = new_sqlite_file(url)
    assert missing == tmp_path / "smart_parking.db"
    notice = new_database_notice(url)
    assert notice is not None and str(missing) in notice
    # Where to look, because a brand-new file is either a first run or a wrong name,
    # and the two are told apart by the setting that produced the name.
    assert "SMART_PARKING_DATABASE_URL" in notice and "configs/app.yaml" in notice

    missing.touch()
    assert new_sqlite_file(url) is None
    assert new_database_notice(url) is None


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "postgresql://u@host/db"])
def test_a_database_with_no_file_has_nothing_to_warn_about(tmp_path, url):
    from smart_parking.db.session import new_database_notice

    assert new_database_notice(url) is None


def test_the_engine_builds_the_directory_but_not_the_database(tmp_path):
    """`data/` is created so SQLite can open the file; the file itself is not.

    Building an engine is lazy, which is what lets a caller ask "was this database
    already here?" after `from_config` and before the first connection.
    """
    from smart_parking.db.session import Database

    cfg = AppConfig(project_root=tmp_path, database=DatabaseConfig(url="sqlite:///data/new.db"))
    database = Database.from_config(cfg)
    assert database.file == tmp_path / "data" / "new.db"
    assert (tmp_path / "data").is_dir()
    assert not database.file.exists()
    assert database.new_file_notice() is not None

    database.create_all()
    assert database.file.exists()
    assert Database.from_config(cfg).new_file_notice() is None
    database.drop_all()


def test_an_existing_database_is_opened_without_a_word_about_it(tmp_path):
    from smart_parking.db.session import Database

    cfg = AppConfig(project_root=tmp_path, database=DatabaseConfig(url="sqlite:///data/runs.db"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "runs.db").touch()
    assert Database.from_config(cfg).new_file_notice() is None


def test_the_deployed_url_is_the_sqlite_file_the_laptop_default_names():
    """One runtime database, agreed between the config and the deployment.

    `deploy/docker-compose.yml` is what a reviewer reads to learn where the records
    live. If its URL ever stops matching `configs/app.yaml`, the compose file is
    advertising one database while the app opens another - which is how the Postgres
    container came to be described as the store of a system that was writing SQLite.
    """
    import yaml

    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
    url = compose["services"]["api"]["environment"]["SMART_PARKING_DATABASE_URL"]
    assert url == load_config(root=ROOT).database.url
    assert url.startswith("sqlite:///"), url


def test_the_postgres_service_is_opt_in_and_no_service_holds_a_password():
    """The container is kept for the day it is needed, and started by nobody today.

    `profiles` is what makes it inert: `docker compose up -d` does not start it, so
    an unused Postgres cannot become a second database by accident. The password it
    would use is a variable, never a value - the file is committed.
    """
    import yaml

    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
    db = compose["services"]["db"]
    assert db["profiles"] == ["postgres"]
    password = db["environment"]["POSTGRES_PASSWORD"]
    assert password.startswith("${POSTGRES_PASSWORD") and ":?" in password


def test_no_module_under_db_names_a_database_file():
    """Which file the database lives in is the config's answer; a quoted `.db`
    path in the layer that reads that config would be a second answer."""
    root = ROOT / "src" / "smart_parking" / "db"
    quoted_db_file = compile_pattern(r"""["'][^"']*\.db["']""")
    offenders = [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        if quoted_db_file.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_database_layer_never_reads_yaml():
    """Config arrives as a `DatabaseConfig`/`AppConfig` object. YAML is the
    loader's business (`core/config.py`), so nothing under `db/` may import it,
    call the loader, or open a file of its own."""
    root = ROOT / "src" / "smart_parking" / "db"
    pattern = compile_pattern(r"\bimport yaml\b|yaml\.(?:safe_)?load|load_config\(|load_slots\(|read_text\(")
    offenders = [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        # The migration runner is the CLI entry point: resolving the URL is its
        # one job, so it is allowed to call the loader - and only through it.
        if path.name != "env.py" and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# -- the migration ----------------------------------------------------------
@pytest.fixture()
def migrated_url(tmp_path, monkeypatch):
    """`alembic upgrade head` against a fresh file, as the CLI would run it.

    env.py reads `SMART_PARKING_DATABASE_URL` when it is set, which is how a test
    migrates a throwaway database without touching configs/app.yaml.
    """
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    monkeypatch.setenv("SMART_PARKING_DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ROOT / "alembic.ini")), "head")
    return url


def schema(engine) -> dict:
    """Everything `inspect()` knows about the Phase 7 tables, in one comparable
    shape. `alembic_version` is excluded: it belongs to the migration runner, and
    a schema built from the models has no reason to carry it."""
    metadata = inspect(engine)
    out: dict[str, dict] = {}
    for table in sorted(TABLES & set(metadata.get_table_names())):
        out[table] = {
            "columns": {
                column["name"]: (str(column["type"]), column["nullable"])
                for column in metadata.get_columns(table)
            },
            "primary_key": tuple(sorted(metadata.get_pk_constraint(table)["constrained_columns"])),
            "foreign_keys": sorted(
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in metadata.get_foreign_keys(table)
            ),
            "unique_constraints": sorted(
                tuple(sorted(unique["column_names"]))
                for unique in metadata.get_unique_constraints(table)
            ),
            "check_constraints": sorted(
                check["name"] or "<anonymous>" for check in metadata.get_check_constraints(table)
            ),
            "indexes": sorted(
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in metadata.get_indexes(table)
            ),
        }
    return out


def test_upgrade_head_creates_every_table_the_models_describe(migrated_url):
    """Nine tables as of Phase 8: the seven from Phase 7 plus `users` and
    `audit_logs`. Which nine is `TABLES` above, and the equality between that set
    and `MIGRATED_TABLES` is guarded in `test_api_config.py` - a model added
    without its migration would otherwise be missing from this comparison too."""
    tables = set(inspect(create_engine(migrated_url)).get_table_names())
    assert TABLES <= tables
    assert TABLES == set(Base.metadata.tables)
    assert "alembic_version" in tables


def test_the_migrated_schema_matches_the_models_exactly(migrated_url):
    built = create_engine("sqlite://")
    Base.metadata.create_all(built)
    assert schema(inspect(create_engine(migrated_url))) == schema(inspect(built))


def test_the_migrations_are_reversible(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'round_trip.db').as_posix()}"
    monkeypatch.setenv("SMART_PARKING_DATABASE_URL", url)
    ini = AlembicConfig(str(ROOT / "alembic.ini"))
    command.upgrade(ini, "head")
    assert TABLES <= set(inspect(create_engine(url)).get_table_names())

    command.downgrade(ini, "base")
    assert set(inspect(create_engine(url)).get_table_names()) == {"alembic_version"}

    command.upgrade(ini, "head")
    assert TABLES <= set(inspect(create_engine(url)).get_table_names())


# -- the constraints the schema claims --------------------------------------
def test_slots_are_unique_per_camera_not_per_database(memory_database):
    """A1 on two cameras is two bays: the doc's numbering restarts per view."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        for camera in ("cam1", "cam2"):
            repos.cameras.get_or_create(camera, name=camera, role="LOT", source="0")
        repos.slots.sync("cam1", [{"code": "A1", "polygon": [[0, 0], [1, 0], [1, 1]]}])
        repos.slots.sync("cam2", [{"code": "A1", "polygon": [[0, 0], [2, 0], [2, 2]]}])
        assert len(repos.slots.for_camera("cam1")) == 1
        assert len(repos.slots.for_camera("cam2")) == 1
        first = repos.slots.find("cam1", "A1")
        assert first.id != repos.slots.find("cam2", "A1").id
        # ...and the pair is what the unique key holds: a hand-written duplicate
        # for one camera is refused even though the code alone is not unique.
        from smart_parking.db.models import ParkingSlot

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(ParkingSlot(camera_id="cam1", slot_code="A1", polygon=[]))


def test_a_second_active_session_for_one_camera_and_track_is_refused(memory_database):
    """A ByteTrack id is not a booking reference; one vehicle cannot be live twice."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        repos.cameras.get_or_create("gate_cam1", name="gate", role="GATE_ENTRY", source="0")
        repos.sessions.open("s-1", "gate_cam1", 11, at(0))
        with pytest.raises(IntegrityError), session.begin_nested():
            repos.sessions.open("s-2", "gate_cam1", 11, at(1))
        assert repos.sessions.get("s-1") is not None
        assert repos.sessions.get("s-2") is None


def test_two_closed_sessions_may_share_one_track_id(memory_database):
    """The index is partial: it binds while a visit is ACTIVE and no longer."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        repos.cameras.get_or_create("gate_cam1", name="gate", role="GATE_ENTRY", source="0")
        repos.sessions.open("s-1", "gate_cam1", 4, at(0))
        repos.sessions.close("s-1", at(30), 30.0, "line")
        repos.sessions.open("s-2", "gate_cam1", 4, at(60))
        assert [row.status for row in repos.sessions.all()] == [
            SessionStatus.COMPLETED,
            SessionStatus.ACTIVE,
        ]


def test_a_second_active_session_for_one_plate_is_refused(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        repos.cameras.get_or_create("gate_cam1", name="gate", role="GATE_ENTRY", source="0")
        plate = repos.plates.record("MH14AB1234", at(0), 0.9)
        first = repos.sessions.open("s-1", "gate_cam1", 1, at(0), plate=plate)
        with pytest.raises(IntegrityError), session.begin_nested():
            repos.sessions.open("s-2", "gate_cam1", 2, at(1), plate=plate)
        assert first.status is SessionStatus.ACTIVE


def test_foreign_keys_are_actually_enforced(memory_database):
    """SQLite ignores them until a connection says otherwise - which is why
    `build_engine` sets the pragma: an unenforced REFERENCES is decoration.

    The write and the read-back are separate units of work, so this also proves
    the rollback: a rejected session leaves nothing half-written behind.
    """
    with pytest.raises(IntegrityError):
        with memory_database.scope() as session:
            session.add(
                ParkingSession(
                    source_session_id="s-x",
                    camera_id="not-a-camera",
                    entry_track_id=1,
                    entry_time=at(0),
                    status=SessionStatus.ACTIVE,
                )
            )
            session.flush()
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        assert repos.sessions.get("s-x") is None
        assert session.scalar(select(func.count()).select_from(ParkingSession)) == 0


def test_a_file_database_writes_in_wal_and_a_memory_one_does_not_try_to(file_database, memory_database):
    """The persistence worker writes while the run's own thread reads, so a file
    database needs WAL: without it the reader waits on the writer's commit lock.

    An in-memory database has no journal to switch, which is the other half of the
    assertion - the pragma is set for the shape that can use it, not blindly.
    """
    with file_database.scope() as session:
        assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    with memory_database.scope() as session:
        assert session.execute(text("PRAGMA journal_mode")).scalar() == "memory"


def test_a_completed_session_cannot_be_recorded_without_both_times(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        repos.cameras.get_or_create("gate_cam1", name="gate", role="GATE_ENTRY", source="0")
        repos.sessions.open("s-1", "gate_cam1", 1, at(0))

    with pytest.raises(IntegrityError):
        with memory_database.scope() as session:
            row = Repositories(session, normalise=str).sessions.get("s-1")
            row.status = SessionStatus.COMPLETED  # no exit time: the CHECK refuses it
            session.flush()

    with memory_database.scope() as session:
        assert Repositories(session, normalise=str).sessions.get("s-1").status is SessionStatus.ACTIVE


def test_a_visit_cannot_exit_before_it_entered(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        repos.cameras.get_or_create("gate_cam1", name="gate", role="GATE_ENTRY", source="0")
        repos.sessions.open("s-1", "gate_cam1", 1, at(10))

    with pytest.raises(IntegrityError):
        with memory_database.scope() as session:
            repos = Repositories(session, normalise=str)
            repos.sessions.close("s-1", at(5), -5.0, "line")
            session.flush()

    with memory_database.scope() as session:
        row = Repositories(session, normalise=str).sessions.get("s-1")
        assert row.exit_time is None and row.duration_seconds is None


# -- Phase 8: the account and audit tables ----------------------------------
def test_an_email_is_unique_in_the_database_and_not_only_in_the_route(memory_database):
    """`POST /users` answers 409 for a duplicate, but two requests can pass that
    check at the same instant. The unique index is the answer that holds when the
    pre-check loses the race, and it holds across the case-folding the repository
    does on the way in."""
    with memory_database.scope() as session:
        Repositories(session, normalise=str).users.create(
            "root@lot.example", "hash-a", Role.ADMIN
        )
    with pytest.raises(IntegrityError):
        with memory_database.scope() as session:
            Repositories(session, normalise=str).users.create(
                "  ROOT@Lot.Example ", "hash-b", Role.VIEWER
            )


def test_a_role_the_token_layer_does_not_know_is_not_a_role(memory_database):
    """Four words are spelled in `Role`, in the JWT claim and in this column's
    CHECK. A fifth value in the database would be read back as a `ValueError` in
    every request that touches the account, so the schema refuses it first."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        for role in Role:
            repos.users.create(f"{role.value.lower()}@lot.example", "hash", role)
        assert repos.users.total() == 4

    with pytest.raises(IntegrityError):
        with memory_database.scope() as session:
            session.add(
                User(email="super@lot.example", password_hash="hash", role="SUPERUSER")
            )
            session.flush()


def test_an_audit_row_records_only_an_action_some_code_writes(memory_database):
    """The list is a tuple in `db/models.py`, so a typo in an endpoint's action
    string is a row that no filter can ever find. The CHECK makes it a failure
    instead of a quiet addition to history."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        row = repos.audit.append(AUDIT_ACTIONS[0], actor_email="root@lot.example")
        assert row.id is not None and row.actor_user_id is None
        assert row.detail == {}, "an audit row with no payload is still a real row"

    with pytest.raises(IntegrityError):
        with memory_database.scope() as session:
            Repositories(session, normalise=str).audit.append(
                "user_delete_and_hard_to_undo", actor_email="root@lot.example"
            )


def test_an_account_with_history_cannot_be_deleted(memory_database):
    """Why `deactivate` exists. `AuditLog.actor_user_id` is a plain foreign key,
    so the database will not let an account carrying its own trail disappear -
    the alternative is an audit row that names nobody."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        actor = repos.users.create("root@lot.example", "hash", Role.ADMIN)
        repos.audit.append("user_created", actor=actor, target_type="user", target_id=str(actor.id))
        actor_id = actor.id

    with pytest.raises(IntegrityError):
        with memory_database.scope() as session:
            session.execute(text("DELETE FROM users WHERE id = :id"), {"id": actor_id})

    with memory_database.scope() as session:
        repos = Repositories(session, normalise=str)
        assert repos.users.get(actor_id) is not None
        assert repos.audit.total() == 1
        assert repos.audit.list()[0].actor_email == "root@lot.example"


# -- shared fixture ---------------------------------------------------------
# `memory_database` (tables from the models, in a shared in-memory database) is
# in conftest.py, because the repository and persistence tests need it too.
