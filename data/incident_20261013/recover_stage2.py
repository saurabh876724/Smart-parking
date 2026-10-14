"""Stage 2: build a clean database from whatever the damaged one gives up.

The damage turned out to be confined to `audit_logs`, so the copy is done per table
and that one table is copied in rowid chunks that bisect down to single rows when a
chunk fails. Losing a page of an append-only audit log is survivable; losing the two
accounts, the ten bays and the four sessions would not be.

Nothing is written to data/smart_parking.db or to original/. The source is the
WAL-replayed copy under work/, the destination is work/recovered.db. Two separate
connections, so every row crosses through Python rather than through SQL - an
`INSERT ... SELECT` would let one bad page abort the whole table.
"""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
SRC = WORK / "replay.db"
DST = WORK / "recovered.db"
CHUNK = 200

for suffix in ("", "-wal", "-shm"):
    for path in (DST,):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()

src = sqlite3.connect(SRC)
dst = sqlite3.connect(DST)

# An empty source would produce an empty destination that passes every check below,
# which is the worst possible outcome of a recovery run: it looks like success.
if not SRC.exists():
    raise SystemExit(f"source {SRC} missing - run recover_stage1.py first")
if not src.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]:
    raise SystemExit("source has no tables - refusing to write a fake empty recovery")

print("=== schema ===")
schema = src.execute(
    "select type, name, sql from sqlite_master where sql is not null order by "
    "case type when 'table' then 0 when 'index' then 1 else 2 end, rowid"
).fetchall()
for kind, name, sql in schema:
    try:
        dst.execute(sql)
        print(f"  ok    {kind:6} {name}")
    except Exception as exc:
        print(f"  FAIL  {kind:6} {name}: {type(exc).__name__}: {exc}")
dst.commit()

tables = [
    r[0]
    for r in src.execute(
        "select name from sqlite_master where type='table' "
        "and name not like 'sqlite_%' and name != 'alembic_version' order by rowid"
    )
]


def column_count(table: str) -> int:
    return len(src.execute(f'select * from "{table}" limit 0').description)


def insert_rows(table: str, rows: list[tuple], rejects: list[str]) -> int:
    """One row at a time, so a single damaged row costs one row and not a table.

    The damaged file turned out to contain NULLs in NOT NULL columns, which is the
    corruption talking rather than the application. Those rows are reported and left
    out; filling them in with a plausible timestamp would be inventing history in the
    one table whose whole job is to be a record of what happened.
    """
    placeholders = ", ".join("?" * column_count(table))
    statement = f'insert into "{table}" values ({placeholders})'
    kept = 0
    for row in rows:
        try:
            dst.execute(statement, row)
            kept += 1
        except sqlite3.IntegrityError as exc:
            keys = ", ".join(repr(v)[:40] for v in row[:3])
            rejects.append(f"first values ({keys}): {exc}")
    return kept


def copy_chunk(table: str, lo: int, hi: int, lost: list[int], rejects: list[str]) -> int:
    """Copy rowids [lo, hi]; a range that raises bisects until single rows fail alone."""
    try:
        rows = src.execute(
            f'select * from "{table}" where rowid between ? and ?', (lo, hi)
        ).fetchall()
    except Exception:
        if lo == hi:
            lost.append(lo)
            return 0
        mid = (lo + hi) // 2
        return (
            copy_chunk(table, lo, mid, lost, rejects)
            + copy_chunk(table, mid + 1, hi, lost, rejects)
        )
    return insert_rows(table, rows, rejects)


report = []
for table in tables:
    lost: list[int] = []
    rejects: list[str] = []
    try:
        total = src.execute(f'select count(*) from "{table}"').fetchone()[0]
    except Exception as exc:
        # A count that fails is damage, not a reason to skip the table - audit_logs is
        # the one table that cannot be counted at all and the one worth the most rows.
        total = None
        print(f"  note: {table} count failed ({type(exc).__name__}), copying anyway")

    if table != "audit_logs":
        try:
            data = src.execute(f'select rowid, * from "{table}"').fetchall()
        except Exception as exc:
            report.append((table, total, 0, [], [], [f"select failed: {type(exc).__name__}"]))
            continue
        copied = insert_rows(table, [row[1:] for row in data], rejects)
        dst.commit()
        report.append((table, total, copied, [], rejects, []))
        continue

    # audit_logs: chunked by rowid, because a single bad page must not lose the log.
    copied = 0
    try:
        start, end = src.execute(f'select min(rowid), max(rowid) from "{table}"').fetchone()
    except Exception as exc:
        # The bounds query is an unbounded scan and that is exactly what the damaged
        # page breaks. A bounded `where rowid between` reads the whole table fine, so
        # a generous fixed range is used instead of giving up on the log.
        start, end = 1, 100_000
        print(f"  note: {table} bounds unreadable ({type(exc).__name__}); scanning rowids 1..{end}")
    if start is not None:
        rowid = start
        while rowid <= end:
            copied += copy_chunk(table, rowid, min(rowid + CHUNK - 1, end), lost, rejects)
            rowid += CHUNK
        dst.commit()
    report.append((table, total, copied, lost, rejects, []))

print("\n=== alembic_version ===")
try:
    version = src.execute("select version_num from alembic_version").fetchone()[0]
    dst.execute("insert into alembic_version (version_num) values (?)", (version,))
    dst.commit()
    print(f"  {version}")
except Exception as exc:
    print(f"  FAILED: {type(exc).__name__}: {exc}")

print("\n=== copy report ===")
print(f"{'table':26}{'source':>9}{'kept':>7}{'rejected':>10}{'unreadable':>12}  note")
for table, total, copied, lost, rejects, notes in report:
    flag = "; ".join(notes)
    print(
        f"{table:26}{str(total):>9}{copied:>7}{len(rejects):>10}{len(lost):>12}  {flag}"
    )
    for line in rejects[:5]:
        print(f"    rejected: {line}")
    if len(rejects) > 5:
        print(f"    ... {len(rejects) - 5} more rejected rows")
    if lost:
        print(f"    unreadable rowids: {lost[:20]}")

print("\n=== PRAGMA integrity_check on recovered.db ===")
print(" ", dst.execute("PRAGMA integrity_check").fetchone()[0])
print("=== PRAGMA foreign_key_check ===")
print(" ", dst.execute("PRAGMA foreign_key_check").fetchall() or "clean")
print("=== audit_logs actions survived ===")
for row in dst.execute("select action, count(*) from audit_logs group by action order by 2 desc"):
    print(f"  {row[0]:20} {row[1]}")

# A b-tree with a shared page hands back rows built from another table's bytes, so
# "inserted without error" is not the same as "is a real row". These are the checks
# that tell the two apart.
print("\n=== sanity of what was kept ===")
known = {r[0] for r in dst.execute("select camera_id from cameras")}
bad = dst.execute(
    "select count(*) from detection_logs where camera_id not in (%s)"
    % ",".join("?" * len(known))
, tuple(known)).fetchone()[0]
print(f"  detection_logs rows with an unknown camera_id: {bad}")
print("  detection_logs event types:",
      {r[0]: r[1] for r in dst.execute(
          "select event_type, count(*) from detection_logs group by event_type")})
print("  detection_logs with a NULL occurred_at:",
      dst.execute("select count(*) from detection_logs where occurred_at is null").fetchone()[0])
print("  parking_slots statuses:",
      {r[0]: r[1] for r in dst.execute(
          "select current_status, count(*) from parking_slots group by current_status")})
print("  users roles:",
      [(r[0], r[1]) for r in dst.execute("select email, role from users")])
print("  audit_logs rows with a NULL created_at:",
      dst.execute("select count(*) from audit_logs where created_at is null").fetchone()[0])
dst.close()
src.close()
