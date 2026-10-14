"""Audit rows: the append-only trail the doc's section D asks for (Phase 8).

Two rules run through this module and both are about trust:

* It only ever **appends**. There is no update and no delete, because a table an
  operator can edit is not evidence, and a row that vanishes when the session it
  described is cleaned up says nothing about what happened.
* The caller decides what goes in `detail`. The API layer passes a dict it has
  already stripped of passwords, hashes and plate text, because an ADMIN-facing
  list endpoint reads this column back verbatim and whatever lands here is
  disclosed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart_parking.db.models import AuditLog, User


class AuditLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        action: str,
        *,
        actor: User | None = None,
        actor_email: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict[str, Any] | None = None,
        source_ip: str | None = None,
    ) -> AuditLog:
        """Write one row.

        `actor` wins over `actor_email` when both are given, so a logged-in call
        cannot be recorded under a different address than its token says. A
        failed login has no `actor` - the account may not exist - which is exactly
        why `actor_email` is kept on the row.
        """
        row = AuditLog(
            action=action,
            actor_user_id=actor.id if actor is not None else None,
            actor_email=actor.email if actor is not None else actor_email,
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
            source_ip=source_ip,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list(
        self,
        *,
        action: str | None = None,
        actor_email: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLog]:
        """Newest first: an audit reader asks "what just happened" before history."""
        query = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        if action is not None:
            query = query.where(AuditLog.action == action)
        if actor_email is not None:
            query = query.where(AuditLog.actor_email == actor_email.strip().lower())
        if from_ts is not None:
            query = query.where(AuditLog.created_at >= from_ts)
        if to_ts is not None:
            query = query.where(AuditLog.created_at <= to_ts)
        return list(self.session.scalars(query.limit(limit).offset(offset)))

    def total(self) -> int:
        return int(self.session.scalar(select(func.count(AuditLog.id))) or 0)

    def count(
        self,
        *,
        action: str | None = None,
        actor_email: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
    ) -> int:
        """The same filter as `list`, counted - so a page's total is the page's rule."""
        query = select(func.count(AuditLog.id))
        if action is not None:
            query = query.where(AuditLog.action == action)
        if actor_email is not None:
            query = query.where(AuditLog.actor_email == actor_email.strip().lower())
        if from_ts is not None:
            query = query.where(AuditLog.created_at >= from_ts)
        if to_ts is not None:
            query = query.where(AuditLog.created_at <= to_ts)
        return int(self.session.scalar(query) or 0)
