"""Run the schema migrations (Phase 7).

    python scripts/migrate.py upgrade head        # create or refresh the database
    python scripts/migrate.py current             # what revision this file is on
    python scripts/migrate.py history
    python scripts/migrate.py check               # models vs database: is a revision needed?
    python scripts/migrate.py revision --autogenerate -m "add tariffs"
    python scripts/migrate.py --url sqlite:///data/scratch.db downgrade base

There is no database URL here by default: `db/migrations/env.py` reads it from
`configs/app.yaml` through the same function the pipeline uses, so a migration and
a run cannot point at different files. `--url` (or `SMART_PARKING_DATABASE_URL`) is
the override for a scratch database, and it is echoed before anything happens,
because `downgrade` destroys rows. A name that has no file behind it yet is called
out in the same breath: `upgrade head` on one creates an empty, fully-migrated
database, which answers every later query with nothing and looks like an application
bug rather than a typo.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from smart_parking.core.config import (  # noqa: E402
    DatabaseConfig,
    database_url_for,
    load_config,
)

from smart_parking.db.session import new_database_notice  # noqa: E402

COMMANDS = ("current", "heads", "history", "upgrade", "downgrade", "check", "revision")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument(
        "revision", nargs="?", default="head",
        help="target revision for upgrade/downgrade (default: head)",
    )
    ap.add_argument("--url", default=None, help="scratch database instead of configs/app.yaml")
    ap.add_argument(
        "--autogenerate", action="store_true",
        help="with 'revision': diff the ORM models against the database",
    )
    ap.add_argument("--rev-id", default=None, help="with 'revision': id to stamp the new file with")
    ap.add_argument("-m", "--message", default=None, help="with 'revision': docstring of the new file")
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    database = DatabaseConfig(url=args.url) if args.url else cfg.database
    url = database_url_for(cfg, database)
    print(f"{args.command}: {url}")
    # The same warning `serve.py` prints, at the moment it matters most: `upgrade
    # head` on a name that does not exist creates an empty, fully-migrated database,
    # which then answers every query with nothing and looks like an application bug.
    if (notice := new_database_notice(url)) is not None:
        print(f"{notice}\n")

    alembic = Config(str(ROOT / "alembic.ini"))
    if args.url:
        # `env.py` honours this variable, and reading it is the one override path
        # that works through the Python API: Alembic's `-x` argument only exists
        # when its own command line built `cmd_opts`.
        os.environ["SMART_PARKING_DATABASE_URL"] = args.url

    if args.command == "upgrade":
        command.upgrade(alembic, args.revision)
    elif args.command == "downgrade":
        command.downgrade(alembic, args.revision)
    elif args.command == "current":
        command.current(alembic)
    elif args.command == "heads":
        command.heads(alembic)
    elif args.command == "history":
        command.history(alembic)
    elif args.command == "check":
        command.check(alembic)
        print("no new upgrade operations detected")
    else:
        command.revision(
            alembic, message=args.message, autogenerate=args.autogenerate, rev_id=args.rev_id
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
