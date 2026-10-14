"""Sessions: history, detail, and the two operator corrections (Phase 8)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from smart_parking.anpr.validation import PlateValidator
from smart_parking.api import deps
from smart_parking.api.deps import ApiError, CurrentUser, ctx_of
from smart_parking.api.schemas import (
    PlateCorrectionRequest,
    SessionCloseRequest,
    SessionPage,
    SessionResponse,
    naive_utc,
)
from smart_parking.api.security import mask_plate
from smart_parking.core.schemas import Role, SessionStatus
from smart_parking.db.base import utcnow

router = APIRouter(prefix="/sessions", tags=["sessions"])
OperatorDep = Annotated[CurrentUser, Depends(deps.require_roles(*deps.WRITE_ROLES))]


def _page_limits(ctx, limit: int | None, offset: int) -> tuple[int, int]:
    """Clamp paging to `api.page_size` so a client cannot ask for the table."""
    upper = ctx.cfg.api.page_size
    size = upper if limit is None else max(1, min(limit, upper))
    return size, max(0, offset)


def _row(repos, session_id: int):
    row = repos.sessions.by_id(session_id)
    if row is None:
        raise ApiError(404, "session_unknown", f"no session {session_id}")
    return row


@router.get("", response_model=SessionPage)
def list_sessions(
    request: Request,
    user: deps.UserDep,
    repos: deps.ReposDep,
    status: SessionStatus | None = None,
    plate: str | None = None,
    camera_id: str | None = None,
    # `from` is a Python keyword, so the parameter carries the name a client sends
    # as an alias rather than making them type `from_=`.
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> SessionPage:
    """Paginated history and the active list, in the doc's shape.

    A `plate=` that matches nothing is an empty page, not a 404: "no visits with
    that number" is a legitimate answer, and the alternative leaks whether a
    plate is in the system to anyone who can call this endpoint.
    """
    ctx = ctx_of(request)
    size, skip = _page_limits(ctx, limit, offset)
    plate_id = None
    if plate:
        found = repos.plates.find(plate)
        if found is None:
            return SessionPage(total=0, limit=size, offset=skip, items=[])
        plate_id = found.id
    rows = repos.sessions.filtered(
        status=status,
        camera_id=camera_id,
        plate_id=plate_id,
        from_ts=naive_utc(from_),
        to_ts=naive_utc(to),
        limit=size,
        offset=skip,
    )
    total = repos.sessions.count_filtered(
        status=status,
        camera_id=camera_id,
        plate_id=plate_id,
        from_ts=naive_utc(from_),
        to_ts=naive_utc(to),
    )
    return SessionPage(
        total=total,
        limit=size,
        offset=skip,
        items=[SessionResponse.from_row(row, reveal_plates=user.can_reveal_plates) for row in rows],
    )


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: int, user: deps.UserDep, repos: deps.ReposDep) -> SessionResponse:
    """Detail for one visit.

    The doc's version carries "images and payment": the evidence crops are not
    stored (a snapshot is taken from the live frame on demand, and a plate crop
    would be a second copy of a picture the stream already serves), and payment is
    a later phase - so this returns what the gate actually measured.
    """
    return SessionResponse.from_row(_row(repos, session_id), reveal_plates=user.can_reveal_plates)


@router.patch("/{session_id}/plate", response_model=SessionResponse)
def correct_plate(
    session_id: int,
    body: PlateCorrectionRequest,
    request: Request,
    user: OperatorDep,
    repos: deps.ReposDep,
) -> SessionResponse:
    """Retype a misread plate. Audited, because this is a person overruling a model.

    The audit detail carries the **masked** before and after: `audit-logs` is an
    ADMIN list endpoint that reads the column back verbatim, and a trail that
    records every plate an operator touched is a plate database with a
    permissions story attached. The full text stays in `license_plates`, where the
    plate rules apply to it.
    """
    ctx = ctx_of(request)
    row = _row(repos, session_id)
    before = row.plate.plate_text if row.plate is not None else None
    # The same validator the OCR stage gates its votes through. An operator's
    # retyping is a plate observation like any other, and a second rule here would
    # be free to accept something the pipeline would have refused.
    outcome = PlateValidator(ctx.cfg.anpr.validation).validate(body.plate)
    if not outcome.accepted:
        raise ApiError(422, f"plate_{outcome.reason}", "that is not a usable plate number")
    plate = repos.plates.record(outcome.plate, seen_at=utcnow())
    repos.sessions.bind_plate(row, plate)
    repos.audit.append(
        "plate_corrected",
        actor=deps.actor(repos, user),
        target_type="session",
        target_id=str(row.id),
        detail={
            "before": mask_plate(before),
            "after": mask_plate(plate.plate_text),
            "session": row.source_session_id,
        },
        source_ip=deps.client_ip(request),
    )
    return SessionResponse.from_row(row, reveal_plates=user.can_reveal_plates)


@router.post("/{session_id}/close", response_model=SessionResponse)
def force_close(
    session_id: int,
    body: SessionCloseRequest,
    request: Request,
    user: OperatorDep,
    repos: deps.ReposDep,
) -> SessionResponse:
    """Close a visit the gate never saw leave.

    Three things this refuses to do: invent an entry time (a session with no
    `entry_time` is an anomaly row, and closing it would need a duration built
    from nothing), claim the gate measured the exit (`exit_reason` stays NULL -
    "line" and "dwell" are the gate's vocabulary, and this is not the gate), and
    leave the human out of the record (`review_reason` says who closed it, and the
    audit row repeats it for the trail).
    """
    row = _row(repos, session_id)
    if row.exit_time is not None:
        raise ApiError(409, "already_closed", f"session {session_id} already has an exit time")
    if row.entry_time is None:
        raise ApiError(
            409,
            "no_entry_time",
            "this session has no measured entry, so a duration would be invented",
        )
    exit_time = naive_utc(body.exit_time) or utcnow()
    if exit_time < row.entry_time:
        raise ApiError(422, "exit_before_entry", "the exit you sent is before the recorded entry")
    duration = (exit_time - row.entry_time).total_seconds()
    row = repos.sessions.close(
        row.source_session_id,
        exit_time,
        duration,
        None,
        status=SessionStatus.COMPLETED,
        review_reason=f"force closed by {user.email}",
    )
    repos.audit.append(
        "session_force_closed",
        actor=deps.actor(repos, user),
        target_type="session",
        target_id=str(row.id),
        detail={"session": row.source_session_id, "duration_seconds": duration, "reason": body.reason},
        source_ip=deps.client_ip(request),
    )
    return SessionResponse.from_row(row, reveal_plates=user.can_reveal_plates)
