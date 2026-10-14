"""Create the first account, or another one (Phase 8).

    python scripts/create_user.py --email admin@example.com --role ADMIN
    python scripts/create_user.py --email op@example.com --role OPERATOR --password '...'
    python scripts/create_user.py --list

The first ADMIN cannot come from `POST /users`, because that endpoint needs an
ADMIN token: a fresh database has no way in. This script is that way in, and it is
the only credential path in the project that runs without one - which is why it
takes the password through `getpass` (or `--password`, for a scripted run) and
prints nothing about it afterwards.

The password goes through the same `PasswordService` the API uses, so the same
policy that rejects a ten-character password at `POST /users` rejects it here, and
the same Argon2id cost parameters land in the row. A CLI that hashed differently
would produce an account that could be created but never logged in to.

This script writes no tables. An unmigrated database is reported with the command
that fixes it (`scripts/migrate.py upgrade head`), because a schema created by
`create_all` here would be a schema Alembic no longer describes.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import inspect  # noqa: E402

from smart_parking.api.security import CredentialError, PasswordService  # noqa: E402
from smart_parking.core.config import DatabaseConfig, load_config  # noqa: E402
from smart_parking.core.schemas import Role  # noqa: E402
from smart_parking.db.repositories import Repositories  # noqa: E402
from smart_parking.db.session import Database  # noqa: E402
from smart_parking.services.persistence import plate_normaliser_for  # noqa: E402


def _password(policy: PasswordService, email: str, given: str | None) -> str:
    """Return a password the policy accepts, or exit with the reason.

    A `--password` sent on a command line is checked here rather than left to
    `policy.hash` inside the write: the failure is the same either way, but the
    traceback version buries the sentence that explains it - and a shell history
    full of rejected attempts is why the message says what to change.
    """
    if given is not None:
        try:
            policy.hash(given, email=email)
        except CredentialError as exc:
            print(f"that password is not usable: {exc}")
            raise SystemExit(1)
        return given
    while True:
        first = getpass.getpass("password (hidden, not echoed): ")
        try:
            policy.hash(first, email=email)
        except CredentialError as exc:
            print(f"  {exc}")
            continue
        if first != getpass.getpass("repeat it: "):
            print("  the two entries did not match; start again")
            continue
        return first


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--email", default=None)
    ap.add_argument("--role", choices=[r.value for r in Role], default=Role.VIEWER.value)
    ap.add_argument(
        "--password", default=None,
        help="omit to be asked for it twice; a value here lands in shell history",
    )
    ap.add_argument("--url", default=None, help="scratch database instead of configs/app.yaml")
    ap.add_argument("--list", action="store_true", help="show the accounts this database has")
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    policy = PasswordService(cfg.api.password)
    database = Database.from_config(cfg, DatabaseConfig(url=args.url) if args.url else None)
    url = database.url

    with database.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    if "users" not in tables:
        print(f"{url}\nhas no users table. Run: python scripts/migrate.py upgrade head")
        return 1

    with database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(cfg))

        if args.list:
            rows = repos.users.all()
            if not rows:
                print("no accounts yet - create one with --email and --role")
            for row in rows:
                state = "" if row.is_active else "  (deactivated)"
                print(f"{row.id:>4}  {row.role.value:<9}  {row.email}{state}")
            return 0

        if not args.email:
            ap.error("--email is required unless --list is given")
        if repos.users.by_email(args.email) is not None:
            print(f"an account for {args.email.strip().lower()} already exists.")
            print("Passwords are not reset from a script: sign in as an ADMIN and "
                  "deactivate this one, then create it again.")
            return 1

        password = _password(policy, args.email, args.password)
        row = repos.users.create(args.email, policy.hash(password, email=args.email), Role(args.role))
        # The trail records a creation made outside the API. `actor_email` is the
        # new address, not the caller's, because the caller is whoever holds this
        # terminal - the row says so in `detail`, which is the honest version.
        repos.audit.append(
            "user_created",
            target_type="user",
            target_id=str(row.id),
            detail={"email": row.email, "role": row.role.value, "via": "scripts/create_user.py"},
        )
        print(f"created user {row.id}: {row.email} ({row.role.value})")
        print(f"database: {database.url}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
