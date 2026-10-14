"""Stage 1 of the incident recovery: replay the WAL on a copy and see what is readable.

Nothing here opens the files under data/ themselves; every connection is to a copy
inside data/incident_20261013/work/, built fresh from original/ each run.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORIG = ROOT / "original"
WORK = ROOT / "work"
NAME = sys.argv[1] if len(sys.argv) > 1 else "replay"


def fresh(name: str) -> Path:
    """A copy of the preserved files under work/, returned as the file to open.

    The `.db` suffix matters: SQLite creates a database silently when it is asked
    to open a path that does not exist, and an empty file passes `integrity_check`.
    """
    target = WORK / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(target) + suffix)
        if path.exists():
            path.unlink()
    for suffix in ("", "-wal", "-shm"):
        src = ORIG / f"smart_parking.db{suffix}"
        if src.exists():
            shutil.copy2(src, Path(str(target) + suffix))
    return target


print(f"sqlite3 library: {sqlite3.sqlite_version}")
path = fresh(NAME)
con = sqlite3.connect(path)
con.row_factory = sqlite3.Row

print("--- PRAGMA journal_mode ---")
try:
    print(con.execute("PRAGMA journal_mode").fetchone()[0])
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")

print("--- integrity_check (after SQLite replays the WAL on open) ---")
try:
    rows = con.execute("PRAGMA integrity_check").fetchall()
    for row in rows[:12]:
        print(" ", row[0])
    print(f"  ({len(rows)} line(s))")
except Exception as exc:
    print(f"  RAISED {type(exc).__name__}: {exc}")

print("--- tables present ---")
try:
    names = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' order by name"
    )]
    print(" ", ", ".join(names))
except Exception as exc:
    print(f"  RAISED {type(exc).__name__}: {exc}")
    names = []

print("--- per-table readability (SELECT COUNT(*)) ---")
readable = []
for table in names:
    try:
        count = con.execute(f'select count(*) from "{table}"').fetchone()[0]
        print(f"  OK    {table:24} {count}")
        readable.append(table)
    except Exception as exc:
        print(f"  ERROR {table:24} {type(exc).__name__}: {exc}")

print("--- alembic revision ---")
try:
    print(" ", con.execute("select version_num from alembic_version").fetchall())
except Exception as exc:
    print(f"  RAISED {type(exc).__name__}: {exc}")

con.close()
print(f"\nreadable tables: {len(readable)}/{len(names)}")
