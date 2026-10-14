"""Shared fixtures: the Phase 7 database and the Phase 8 HTTP surface.

Two shapes of database, because the phases have two shapes to test. An in-memory
one for everything that writes and reads from a single thread, and a file one for
the consumers that write from their own thread while the calling thread reads the
result back - the persistence worker, and the API's request session. `Database.
create_all` is documented as being for throwaway databases only; the migrations
themselves are tested against a file in `test_db_schema.py`, so nothing here
depends on skipping that step.

The API fixtures are here rather than in one test file because four files use
them, and because the shape of "a live app over a seeded database" is the same
each time: `app` builds, `client` opens its lifespan, `tokens` logs in, `api`
makes the calls.

Three knobs are set once, for reasons that outlive this file:

* **Argon2 at its floor** (8 MiB, one pass, one lane). The production cost is
  ~55 ms per login, which is the point of a KDF and not the point of a suite that
  hashes a password forty times.
* **No streaming cameras.** `start_pipelines=False` keeps YOLO out of the process;
  the cameras that do get exercised are driven through `CameraRunner` directly, in
  `test_api_live.py`, where the test can wait for them.
* **A test secret passed to `create_app`.** `jwt_secret_for` deliberately refuses
  to start without a key, so the alternative would be reading `data/.secret` - the
  developer's own key - from a test.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from smart_parking.api.app import create_app
from smart_parking.api.security import PasswordService
from smart_parking.core.config import CameraRole, DatabaseConfig, PasswordConfig, load_config
from smart_parking.core.events import RecordingEventBus
from smart_parking.core.schemas import Role, SessionStatus, SlotStatus, SlotType
from smart_parking.db.base import utcnow
from smart_parking.db.repositories import Repositories
from smart_parking.db.session import Database
from smart_parking.services.persistence import plate_normaliser_for

ROOT = Path(__file__).resolve().parents[2]

# 48 bytes: over TokenIssuer's 32-byte floor, and long enough that no test can
# mistake "this key is short" for "this key is wrong".
TEST_SECRET = "unit-test-signing-key-0123456789abcdef-0123456789ab"

# Argon2's own floor. Deliberately not the defaults, which cost ~55 ms a hash.
CHEAP_PASSWORDS = PasswordConfig(memory_kib=8192, time_cost=1, parallelism=1)

# Every password here clears the length policy and contains no part of its own
# address, so a refusal in a test is the rule under test and not the fixture.
ACCOUNTS: dict[Role, tuple[str, str]] = {
    Role.ADMIN: ("root@lot.example", "Gatehouse-Keystone-3"),
    Role.OPERATOR: ("desk@lot.example", "Barrier-Ticket-2026"),
    Role.VIEWER: ("board@lot.example", "Display-Wall-4471"),
    Role.CUSTOMER: ("driver@lot.example", "Windy-Aisle-9-2026"),
}


@pytest.fixture()
def memory_database():
    cfg = load_config(root=ROOT)
    database = Database.from_config(cfg, DatabaseConfig(url="sqlite://"))
    database.create_all()
    yield database
    database.engine.dispose()


@pytest.fixture()
def file_database(tmp_path):
    """The same schema on a file, in a directory pytest cleans up.

    An in-memory database is one connection shared by every thread; a file
    database is a pool of them, which is what a real run against
    `data/smart_parking.db` actually is. A test that starts the persistence
    worker therefore wants this one, or it would be exercising a connection
    shape production never uses.
    """
    cfg = load_config(root=ROOT)
    url = f"sqlite:///{(tmp_path / 'persistence_test.db').as_posix()}"
    database = Database.from_config(cfg, DatabaseConfig(url=url))
    database.create_all()
    yield database
    database.engine.dispose()


# -- Phase 8: the application ------------------------------------------------


@pytest.fixture()
def api_config(tmp_path):
    """The shipped config with the three things a test must not inherit.

    Loaded from `configs/app.yaml` rather than written out, so a test that asserts
    `page_size` or `read_plates` is asserting the file an operator actually edits.
    The database moves to a tmp file, Argon2 drops to its floor, and the pipeline
    block is emptied so an app built from this config starts no camera threads.
    """
    cfg = load_config(root=ROOT)
    url = f"sqlite:///{(tmp_path / 'api_test.db').as_posix()}"
    return cfg.model_copy(
        update={
            "database": DatabaseConfig(url=url),
            "api": cfg.api.model_copy(
                update={
                    "password": CHEAP_PASSWORDS,
                    "pipeline": cfg.api.pipeline.model_copy(update={"enabled": False, "cameras": []}),
                }
            ),
        }
    )


@pytest.fixture()
def api_database(api_config):
    """A migrated-by-models file database, on the URL `api_config` names.

    A file, not `sqlite://`, because the request's session and a WebSocket's
    authentication run on the portal's thread while the test thread reads the
    same rows back - one connection shared across threads would be a shape this
    project's production path never uses.
    """
    database = Database.from_config(api_config, api_config.database)
    database.create_all()
    yield database
    database.engine.dispose()


@pytest.fixture()
def seeded(api_database, api_config):
    """Four accounts, two cameras, four bays and four visits - committed, then handed over.

    Times are offsets from `utcnow()` rather than a fixed date so the default
    24-hour statistics window contains them: an endpoint whose "last day" answer
    is empty because the fixture is from last year tests nothing.

    The seeding transaction is closed before the fixture yields. Yielding from
    inside the `with` would leave the rows uncommitted while the test ran, and a
    file database shows another connection - which is what every request's
    session is - nothing at all until the writer commits.
    """
    normalise = plate_normaliser_for(api_config)
    passwords = PasswordService(api_config.api.password)
    now = utcnow()

    def at(minutes: float) -> datetime:
        return now - timedelta(minutes=minutes)

    with api_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        user_ids: dict[str, int] = {}
        for role, (email, password) in ACCOUNTS.items():
            row = repos.users.create(email, passwords.hash(password, email=email), role)
            user_ids[role.value] = row.id

        repos.cameras.upsert(
            "lot_cam1",
            name="Lot camera 1",
            role=CameraRole.LOT,
            source="data/samples/lot_shot1.mp4",
            slots_file="configs/slots/lot_a_cam1.json",
        )
        repos.cameras.upsert(
            "gate_cam1",
            name="Gate camera 1",
            role=CameraRole.GATE_ENTRY,
            source="data/samples/gate_traffic.mp4",
            gate_file="configs/gates/gate_a.json",
        )

        bays = {
            "A1": (SlotStatus.FREE, SlotType.REGULAR, None),
            "A2": (SlotStatus.OCCUPIED, SlotType.REGULAR, 11),
            "A3": (SlotStatus.UNKNOWN, SlotType.REGULAR, None),
            "A4": (SlotStatus.FREE, SlotType.RESERVED, None),
        }
        slot_ids: dict[str, int] = {}
        for row in repos.slots.sync(
            "lot_cam1",
            [
                {
                    "code": code,
                    "polygon": [[10, 10], [110, 10], [110, 60], [10, 60]],
                    "type": slot_type.value,
                }
                for code, (_, slot_type, _) in bays.items()
            ],
            frame_size={"width": 480, "height": 360},
        ):
            status, _, track_id = bays[row.slot_code]
            repos.slots.set_status(row, status, at(90), track_id)
            slot_ids[row.slot_code] = row.id

        plate_ids: dict[str, int] = {}
        for text, confidence, reads in (
            ("MH20AB1234", 0.93, 3),
            ("DL01CK9999", 0.88, 1),
            ("MH14ZZ1111", 0.61, 1),
        ):
            plate = repos.plates.record(text, seen_at=at(30), confidence=confidence)
            for _ in range(reads - 1):
                plate = repos.plates.record(text, seen_at=at(20), confidence=confidence)
            plate_ids[text] = plate.id

        car = repos.vehicles.record(repos.plates.get(plate_ids["MH20AB1234"]), "car")

        # Four visits: one live, two closed (so a median has more than one number
        # to work with), and an orphan exit whose duration nobody measured.
        active = repos.sessions.open(
            "gate-0001",
            "gate_cam1",
            11,
            at(120),
            plate=repos.plates.get(plate_ids["MH20AB1234"]),
            vehicle=car,
            ocr_confidence=0.91,
            plate_confidence=0.87,
        )
        repos.sessions.bind_slot(active, slot_ids["A2"])
        short = repos.sessions.open("gate-0002", "gate_cam1", 12, at(360))
        repos.sessions.bind_plate(short, repos.plates.get(plate_ids["DL01CK9999"]))
        repos.sessions.close("gate-0002", at(330), 1800.0, "line")
        long = repos.sessions.open("gate-0003", "gate_cam1", 14, at(480))
        repos.sessions.bind_plate(long, repos.plates.get(plate_ids["MH14ZZ1111"]))
        repos.sessions.close("gate-0003", at(420), 3600.0, "dwell")
        orphan = repos.sessions.record_orphan_exit(
            "gate-0004", "gate_cam1", 15, at(180), "dwell", "exit_without_entry"
        )

        # One interval still open (A2 is occupied now) and one closed cycle, so a
        # history chart has both kinds of row to disagree about.
        repos.occupancy.open_interval(repos.slots.get(slot_ids["A2"]), at(120), track_id=11, session_id=active.id)
        repos.occupancy.open_interval(repos.slots.get(slot_ids["A1"]), at(600))
        repos.occupancy.close_interval(slot_ids["A1"], at(540), session_id=short.id)

        data = SimpleNamespace(
            now=now,
            at=at,
            accounts=ACCOUNTS,
            user_ids=user_ids,
            slot_ids=slot_ids,
            plate_ids=plate_ids,
            session_ids={
                "active": active.id,
                "short": short.id,
                "long": long.id,
                "orphan": orphan.id,
            },
        )

    yield data


@pytest.fixture()
def bus():
    """The event bus the app is built on, so a test can publish into a socket."""
    return RecordingEventBus()


@pytest.fixture()
def make_client(api_config, api_database, seeded, bus):
    """Open a live app, optionally with the `ApiConfig` rewritten by the test.

    `update` receives the fixture's `ApiConfig` and returns the one to run with,
    which is how a test of `ws_queue_size` or `page_size` changes the rule without
    rebuilding the whole app by hand. Every client is closed here, so a lifespan
    that fails to stop a thread shows up as a hang rather than a leak.
    """
    opened: list[TestClient] = []

    def build(update=None, *, cfg_update=None, **kwargs) -> TestClient:
        cfg = api_config
        if update is not None:
            cfg = cfg.model_copy(update={"api": update(cfg.api)})
        if cfg_update is not None:
            cfg = cfg.model_copy(update=cfg_update(cfg))
        kwargs.setdefault("bus", bus)
        kwargs.setdefault("secret", TEST_SECRET)
        kwargs.setdefault("start_pipelines", False)
        app = create_app(cfg, api_database, **kwargs)
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client

    yield build
    for client in reversed(opened):
        client.__exit__(None, None, None)


@pytest.fixture()
def client(make_client):
    return make_client()


def tokens_for(client: TestClient) -> dict[str, str]:
    """A real access token per role, obtained by logging in over HTTP.

    A function rather than only a fixture because a test that builds its own
    client - one with a different `ApiConfig` - needs the same tokens for it.
    """
    prefix = client.app.state.ctx.cfg.api.prefix
    out: dict[str, str] = {}
    for role, (email, password) in ACCOUNTS.items():
        response = client.post(f"{prefix}/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        out[role.value] = response.json()["access_token"]
    return out


@pytest.fixture()
def tokens(client):
    return tokens_for(client)


class Api:
    """`/api/v1` paths, a role name instead of a header, and the client beneath.

    Thin on purpose: it exists so sixty assertions do not repeat the prefix and
    the word "Bearer", not to hide the transport. Anything unusual - no header, a
    hand-made token, a WebSocket - goes through `api.client`.
    """

    def __init__(self, client: TestClient, tokens: dict[str, str]) -> None:
        self.client = client
        self.tokens = tokens
        self.prefix = client.app.state.ctx.cfg.api.prefix
        self.ctx = client.app.state.ctx

    def headers(self, role: str | None, token: str | None, extra: dict | None) -> dict[str, str]:
        out = dict(extra or {})
        if token is not None:
            out["Authorization"] = f"Bearer {token}"
        elif role is not None:
            out["Authorization"] = f"Bearer {self.tokens[role]}"
        return out

    def request(
        self,
        verb: str,
        path: str,
        *,
        role: str | None = "ADMIN",
        token: str | None = None,
        headers: dict | None = None,
        **kwargs,
    ):
        return self.client.request(
            verb, self.prefix + path, headers=self.headers(role, token, headers), **kwargs
        )

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request("PATCH", path, **kwargs)

    def ws_ticket(self, *, role: str = "ADMIN", token: str | None = None) -> str:
        """Mint a one-time socket ticket over HTTP, the way a dashboard does.

        A fresh call per socket, because a ticket is spent by the first connect
        that presents it: a fixture that handed the same ticket to two viewers
        would be testing reuse, not authentication.
        """
        response = self.post("/auth/ws-ticket", role=role, token=token)
        assert response.status_code == 200, response.text
        return response.json()["ticket"]

    def ws(self, path, *, ticket: str | None = None, token: str | None = None, role: str | None = None):
        """A WebSocket URL, with the credential in the query string.

        `role` mints a ticket for that account and `ticket` presents one verbatim;
        both put it there as `?ticket=`. `token` puts a bearer token there as
        `?token=`, which is the shape the sockets refuse - kept because a test has
        to be able to build the URL it is asserting against.

        No `api.prefix` either way, deliberately: the doc names its sockets at the
        root and `app.py` mounts `WEBSOCKET_ROUTERS` without the prefix the REST
        routers get. A test that added it would be refused by the router with a
        1000 close, which looks exactly like an auth bug.
        """
        if role is not None and ticket is None:
            ticket = self.ws_ticket(role=role)
        if ticket is not None:
            query = f"ticket={ticket}"
        elif token is not None:
            query = f"token={token}"
        else:
            return path
        join = "&" if "?" in path else "?"
        return path + f"{join}{query}"


@pytest.fixture()
def api(client, tokens) -> Api:
    return Api(client, tokens)
