"""Slots and the per-lot counters (doc section E, "Slots")."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from smart_parking.api import deps
from smart_parking.api.deps import ApiError, CurrentUser
from smart_parking.api.schemas import (
    LotSummaryResponse,
    PolygonUpdateRequest,
    SlotCreateRequest,
    SlotResponse,
)
from smart_parking.core.schemas import Role, SlotStatus, SlotType

router = APIRouter(tags=["slots"])
AdminDep = Annotated[CurrentUser, Depends(deps.require_roles(Role.ADMIN))]
OperatorDep = Annotated[CurrentUser, Depends(deps.require_roles(*deps.WRITE_ROLES))]


@router.get("/slots", response_model=list[SlotResponse])
def list_slots(
    user: deps.UserDep,
    repos: deps.ReposDep,
    camera_id: str | None = None,
    status: SlotStatus | None = None,
    type: SlotType | None = None,  # noqa: A002 - the doc's own query name
) -> list[SlotResponse]:
    """Every bay, optionally narrowed by camera, committed status or type.

    `status` is the FSM's **committed** state. The PENDING_* states live in the
    engine's debounce and are never written here, so a row cannot be
    "PENDING_OCCUPIED" - which is why a dashboard wanting the in-between picture
    reads the WebSocket rather than this table.
    """
    rows = repos.slots.filtered(camera_id=camera_id, status=status, slot_type=type)
    active = repos.sessions.active_slot_map()
    return [
        SlotResponse.from_row(row, session_id=active.get(row.id)) for row in rows
    ]


@router.post("/slots", response_model=SlotResponse, status_code=201)
def create_slot(
    body: SlotCreateRequest,
    request: Request,
    user: AdminDep,
    repos: deps.ReposDep,
) -> SlotResponse:
    """Add one bay by hand.

    ADMIN only, as the doc has it: a polygon decides which car counts as parked
    where, so drawing one is a configuration change, not data entry. This is the
    endpoint behind a browser's "draw a slot" tool; `scripts/calibrate.py` writes
    the same geometry through the calibration file instead.

    Audited, because a bay drawn here is not a bay the calibration file knows
    about, and the next `--persist` pipeline run will not refresh it from a
    drawing nobody saved. That difference is a fact worth a trail.
    """
    if repos.cameras.get(body.camera_id) is None:
        raise ApiError(404, "camera_unknown", f"no camera {body.camera_id!r} is registered")
    if repos.slots.find(body.camera_id, body.slot_code) is not None:
        raise ApiError(
            409,
            "slot_exists",
            f"camera {body.camera_id!r} already has a bay {body.slot_code!r}",
        )
    rows = repos.slots.sync(
        body.camera_id,
        [{"code": body.slot_code, "polygon": body.polygon, "type": body.slot_type}],
        frame_size=body.frame_size,
    )
    row = rows[0]
    repos.audit.append(
        "slot_created",
        actor=deps.actor(repos, user),
        target_type="slot",
        target_id=str(row.id),
        detail={"camera_id": row.camera_id, "slot_code": row.slot_code, "points": len(body.polygon)},
        source_ip=deps.client_ip(request),
    )
    return SlotResponse.from_row(row)


@router.put("/slots/{slot_id}/polygon", response_model=SlotResponse)
def update_polygon(
    slot_id: int,
    body: PolygonUpdateRequest,
    request: Request,
    user: OperatorDep,
    repos: deps.ReposDep,
) -> SlotResponse:
    """Redraw a bay, audited as `slot_polygon_updated`.

    The status is left alone deliberately: the bay is still whatever it was a
    second ago, and the next FSM tick will re-decide it against the new shape.
    Rewriting the status here would be this endpoint inventing an observation.
    """
    row = repos.slots.get(slot_id)
    if row is None:
        raise ApiError(404, "slot_unknown", f"no slot {slot_id}")
    # The polygon is in the frame's pixels, so a new shape without the size it
    # was drawn on is a shape nobody can rescale correctly later.
    repos.slots.set_polygon(row, body.polygon, body.frame_size)
    repos.audit.append(
        "slot_polygon_updated",
        actor=deps.actor(repos, user),
        target_type="slot",
        target_id=str(row.id),
        detail={"camera_id": row.camera_id, "slot_code": row.slot_code, "points": len(body.polygon)},
        source_ip=deps.client_ip(request),
    )
    return SlotResponse.from_row(row)


@router.get("/lots/{camera_id}/summary", response_model=LotSummaryResponse)
def lot_summary(camera_id: str, user: deps.UserDep, repos: deps.ReposDep) -> LotSummaryResponse:
    """Counts for one lot, where the lot is identified by its camera.

    `parking_lots` is not a table this system has yet, and inventing one to hold
    nothing but a name would make `camera_id` mean two things at once. The
    doc's `reserved` is reported as the number of bays whose **type** is RESERVED:
    the occupancy engine has no RESERVED state, and a status-shaped number for it
    would be a number nothing measures.
    """
    if repos.cameras.get(camera_id) is None:
        raise ApiError(404, "camera_unknown", f"no camera {camera_id!r} is registered")
    rows = repos.slots.filtered(camera_id=camera_id)
    occupied = sum(1 for row in rows if row.current_status is SlotStatus.OCCUPIED)
    available = sum(1 for row in rows if row.current_status is SlotStatus.FREE)
    unknown = sum(1 for row in rows if row.current_status is SlotStatus.UNKNOWN)
    reserved = sum(1 for row in rows if row.slot_type is SlotType.RESERVED)
    committed = occupied + available
    return LotSummaryResponse(
        camera_id=camera_id,
        total=len(rows),
        occupied=occupied,
        available=available,
        reserved=reserved,
        unknown=unknown,
        occupancy_rate=(occupied / committed) if committed else 0.0,
    )
