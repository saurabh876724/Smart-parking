"""Serve the Phase 8 API (FastAPI + uvicorn).

    python scripts/serve.py                                   # http://127.0.0.1:8000
    python scripts/serve.py --persist                         # cameras' events also reach SQLite
    python scripts/serve.py --no-pipelines                    # reads and writes only, no threads
    python scripts/serve.py --host 0.0.0.0 --port 8080        # reachable from a phone on the LAN
    python scripts/serve.py --url sqlite:///data/scratch_api.db

Three checks run before the port opens, and each one prints the command that fixes
it rather than failing with a traceback: is there a signing key (`dev_secret.py`),
is the database migrated (`migrate.py`), and is there at least one account to log in
with (`create_user.py`). A server that starts and then answers 401 forever is worse
than one that refuses to start.

**And a fourth, after those: is something else already writing this file.** See
`shared_database_conflicts`. Two processes sharing one SQLite database is not a slow
path, it is a corrupted database, and on this project the second process is usually a
container the operator cannot see.

**What this starts.** `--persist` and the camera threads are the two things that
touch hardware or a database, so they are both opt-in from the command line and
both reported in the banner. The cameras are the clips named in
`configs/app.yaml -> api.pipeline`: one real lot recording and one rendered gate
fixture. They are replays, and the banner says so on its own line - a served stream
is the one part of this system that a reader could mistake for a deployed camera.

`--reload` is absent on purpose: uvicorn's reloader imports the app in a subprocess
it manages, and a second process decoding the same clips is a second GPU context on
a 6 GB card.

**What this writes to the log.** uvicorn's access line quotes the request URL with
its query string, and two of this API's credentials can only travel in a query
string: a socket's `?ticket=` and an `<img>` stream's `?token=`. `uvicorn_log_config`
hangs `api/access_log.py`'s filter on the access logger so the values are replaced
by `***` before the line reaches stdout, the same job `deploy/nginx-http.conf`'s
`sp_no_query` log format does one hop earlier.
"""

from __future__ import annotations

import argparse
import copy
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from smart_parking.api.access_log import CredentialMaskFilter  # noqa: E402
from smart_parking.api.app import create_app  # noqa: E402
from smart_parking.core.config import (  # noqa: E402
    DatabaseConfig,
    SecretUnavailable,
    jwt_secret_for,
    load_config,
)
from smart_parking.db.models import MIGRATED_TABLES  # noqa: E402
from smart_parking.db.repositories import Repositories  # noqa: E402
from smart_parking.db.session import Database  # noqa: E402
from smart_parking.services.persistence import plate_normaliser_for  # noqa: E402


MASKED_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn")


def uvicorn_log_config() -> dict:
    """uvicorn's own logging config, with the access line scrubbed of credentials.

    The default config is copied rather than re-declared so the format, the handler
    and the level stay whatever uvicorn's version ships - this only hangs one filter
    on the loggers that write a request URL. Without it, `docker compose logs api`
    keeps a copy of every `?ticket=` and `?token=` that connected, which is the thing
    `deploy/nginx-http.conf`'s log format exists to stop on the proxy side.

    Three loggers, not the one obvious. `uvicorn.access` writes the HTTP request line;
    the WebSocket handshake is written by **`uvicorn.error`**, which is where the
    credential actually lands:

        INFO: 127.0.0.1:50853 - "WebSocket /ws/live?ticket=…" [accepted]
        INFO: 127.0.0.1:50856 - "WebSocket /ws/live?ticket=…" 403

    Both of those came from `uvicorn.protocols.websockets.*.asgi_send()` logging
    `get_path_with_query_string(self.scope)` on `self.logger`, which is
    `logging.getLogger("uvicorn.error")`. A filter on the access logger alone masked
    the browser's page requests and left every socket handshake ticket in the log -
    which is the opposite of what the filter is for, and was found by pointing one at
    a running server and reading the file back. `uvicorn` itself is included because
    a record emitted on a child logger is filtered by that child only, and a future
    version that moves the line up the tree should not silently unmask it.
    """
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["filters"] = {"credentials": {"()": CredentialMaskFilter}}
    for name in MASKED_LOGGERS:
        logger = config["loggers"].setdefault(name, {})
        logger["filters"] = [*(logger.get("filters") or []), "credentials"]
    return config


COMPOSE_API_CONTAINER = "smart-parking-api"


def port_in_use(host: str, port: int, timeout: float = 0.75) -> bool:
    """Can a TCP connection already be made to the address this server wants?

    A connect rather than a bind, because on Windows a bind to an address another
    socket is already listening on succeeds - the two coexist, and requests to the
    loopback address go to whichever is more specific. That is precisely how a
    container publishing 8000 and a host server opening 8000 both "worked", so the
    port cannot be trusted to refuse the second one and has to be asked instead.
    """
    target = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def running_containers() -> list[str]:
    """Names from `docker ps`, or nothing when Docker is absent, stopped or slow.

    Every failure here is a silence rather than an error: a machine with no Docker
    must still be able to start the API, and a daemon that does not answer in two
    seconds is not worth blocking a development server for.
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def shared_database_conflicts(
    *, host: str, port: int, database_file: Path | None, root: Path, allow_shared_db: bool
) -> list[str]:
    """Reasons not to open this database right now, each naming the fix.

    The shape of the failure this refuses is the one that destroys data rather than
    the one that annoys: SQLite's locking is per operating system, so a Windows
    process and a Linux container reaching the same file through a bind mount each
    see an unlocked database and both write. The result is a page belonging to two
    tables at once, and the database stops opening at all.

    The container is invisible from the host - it is not a process `psutil` can list
    and it does not hold a Windows file handle - so it is detected by what it leaves
    behind: the port it published, and its own name in `docker ps` beside the exact
    file this run is about to open.
    """
    if allow_shared_db:
        return []
    reasons: list[str] = []
    if port_in_use(host, port):
        reasons.append(
            f"something is already answering on {host}:{port}, so this server would "
            "share the address rather than take it (Windows lets both bind, and the "
            "requests go to whichever is more specific).\n"
            "  Stop it, or pick another port: python scripts/serve.py --port 8010\n"
            "  If it is the Docker stack: cd deploy && docker compose down"
        )
    if database_file is not None and database_file == (root / "data" / "smart_parking.db").resolve():
        if COMPOSE_API_CONTAINER in running_containers():
            reasons.append(
                f"{COMPOSE_API_CONTAINER} is running and mounts this project's data/ "
                "directory, so it writes the same file this process is about to open.\n"
                "  cd deploy && docker compose down\n"
                "  - or work on a copy: python scripts/serve.py --url "
                "sqlite:///data/scratch_api.db\n"
                "Two writers on one SQLite file is not a slow path; it is a corrupted "
                "database. --allow-shared-db overrides this only when the other writer "
                "is known and stopped."
            )
    return reasons


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--url", default=None, help="scratch database instead of configs/app.yaml")
    ap.add_argument("--persist", action="store_true", help="write the cameras' events to the database")
    ap.add_argument("--no-pipelines", action="store_true", help="start no camera threads")
    ap.add_argument(
        "--allow-shared-db",
        action="store_true",
        help="start even though another writer appears to hold this database",
    )
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    database = Database.from_config(cfg, DatabaseConfig(url=args.url) if args.url else None)

    # Which file this is, said out loud before anything else reads it: SQLite creates
    # a database it has never seen, so a wrong name opens an empty one and every
    # later check - no tables, no accounts - is a symptom rather than a cause.
    if (notice := database.new_file_notice()) is not None:
        print(f"{notice}\n")

    try:
        jwt_secret_for(cfg)
    except SecretUnavailable as exc:
        print(f"{exc}\n\n  python scripts/dev_secret.py")
        return 1

    check = database.check(MIGRATED_TABLES)
    if not check["reachable"]:
        print(f"cannot open {database.url}: {check['error']}\n")
        return 1
    if check["missing_tables"]:
        print(
            f"{database.url} is missing {', '.join(check['missing_tables'])}.\n"
            "  python scripts/migrate.py upgrade head\n"
        )
        return 1

    with database.scope() as session:
        accounts = Repositories(session, normalise=plate_normaliser_for(cfg)).users.total()
    if accounts == 0:
        print(
            "this database has no accounts, so every endpoint except /auth/login will "
            "answer 401 forever:\n"
            "  python scripts/create_user.py --email you@example.com --role ADMIN\n"
        )
        return 1

    conflicts = shared_database_conflicts(
        host=args.host,
        port=args.port,
        database_file=database.file,
        root=ROOT,
        allow_shared_db=args.allow_shared_db,
    )
    if conflicts:
        for reason in conflicts:
            print(f"{reason}\n")
        return 1

    app = create_app(
        cfg,
        database,
        persist=args.persist,
        start_pipelines=None if not args.no_pipelines else False,
    )
    print(f"serving {cfg.api.prefix} on http://{args.host}:{args.port}  (docs at /docs)")
    print("log in:   curl -X POST http://127.0.0.1:%d%s/auth/login "
          "-H 'content-type: application/json' -d '{\"email\":\"…\",\"password\":\"…\"}'"
          % (args.port, cfg.api.prefix))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        log_config=uvicorn_log_config(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
