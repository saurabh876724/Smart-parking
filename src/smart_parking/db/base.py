"""Declarative base and the naming convention every constraint inherits.

Explicit names are not decoration: SQLite cannot rename or drop a constraint
that Alembic has to refer to by name in a later migration, and an unnamed one
gets a system-generated label that changes when a column is reordered. Naming
them here once means the same models migrate cleanly on SQLite and Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc(timestamp: float | None) -> datetime | None:
    """A domain timestamp (epoch seconds) as a stored datetime.

    The domain works in epoch floats - video time on replay, wall clock live -
    and the database works in datetimes. SQLite has no timezone-aware storage:
    a value bound with a tzinfo is written as its own fields and read back
    naive, which would make "UTC" a guess. So every datetime in this schema is
    **naive UTC**, converted here on the way in and back with `to_epoch` on the
    way out, and the round trip is exact to the microsecond.
    """
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)


def to_epoch(value: Any) -> float | None:
    """The inverse of `to_utc`, for tests and for feeding the domain back."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).timestamp()


class TimestampMixin:
    """created_at / updated_at, as the spec's model list requires."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
