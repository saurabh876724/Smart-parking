"""Audit trail reader (doc section E: `GET /audit-logs`, ADMIN)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from smart_parking.api import deps
from smart_parking.api.deps import ApiError, CurrentUser, ctx_of
from smart_parking.api.schemas import AuditPage, AuditResponse, naive_utc
from smart_parking.core.schemas import Role
from smart_parking.db.models import AUDIT_ACTIONS

router = APIRouter(prefix="/audit-logs", tags=["audit"])
AdminDep = Annotated[CurrentUser, Depends(deps.require_roles(Role.ADMIN))]


@router.get("", response_model=AuditPage)
def list_audit(
    request: Request,
    user: AdminDep,
    repos: deps.ReposDep,
    action: str | None = None,
    actor_email: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> AuditPage:
    """The trail, newest first, filtered the way an operator actually asks.

    ADMIN only because this table says who did what to whom: read alongside
    `license_plates` it reconstructs an operator's shift, which is not something
    every dashboard role is entitled to see.

    An unknown `action` is refused rather than answered with an empty page - the
    vocabulary is a database constraint, and a filter that silently matches
    nothing is how a typo becomes "nobody did that".
    """
    ctx = ctx_of(request)
    if action is not None and action not in AUDIT_ACTIONS:
        raise ApiError(
            422,
            "unknown_action",
            f"action must be one of {', '.join(AUDIT_ACTIONS)}",
        )
    size = ctx.cfg.api.page_size if limit is None else max(1, min(limit, ctx.cfg.api.page_size))
    skip = max(0, offset)
    rows = repos.audit.list(
        action=action,
        actor_email=actor_email,
        from_ts=naive_utc(from_),
        to_ts=naive_utc(to),
        limit=size,
        offset=skip,
    )
    total = repos.audit.count(
        action=action, actor_email=actor_email, from_ts=naive_utc(from_), to_ts=naive_utc(to)
    )
    return AuditPage(total=total, limit=size, offset=skip, items=[AuditResponse.from_row(r) for r in rows])
