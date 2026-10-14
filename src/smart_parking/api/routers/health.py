"""`/health`: the three questions a deployment asks about itself.

Is the database there and migrated, is there a GPU, and are the cameras that
were supposed to start actually producing frames. Everything else an operator
wants is a real endpoint; this one exists so `scripts/serve.py`'s startup report
and a monitoring check ask the same question the same way.

It is authenticated. The payload names cameras, quotes the exact error a pipeline
thread died with (which includes file paths), and counts WebSocket clients -
useful to an operator, and a map of the machine to anyone else. A login page has
to work before a token exists, so the unauthenticated surface is `/auth/login`
and `/auth/refresh` and nothing more here.

No table is queried by hand: `Database.check` reflects, so a missing migration is
reported by name rather than raising from the first `SELECT` this endpoint would
otherwise have written.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from smart_parking.api import deps
from smart_parking.api.deps import AppContext, CurrentUser, ctx_of
from smart_parking.api.schemas import CameraLiveResponse, HealthResponse
from smart_parking.db.models import MIGRATED_TABLES

router = APIRouter(tags=["health"])

# A database that answers and a camera that is quiet are different findings, and
# only the first one is an outage: `degraded` is the word for "up, but read the
# fields" so a monitoring rule can page on `status` and still show the reason.


def gpu_info() -> dict[str, Any]:
    """What torch says about this machine, without importing torch at module load.

    The API package has to stay importable on a box with no CUDA stack (a CI job
    that only runs the schema tests, a laptop without the GPU extra installed), so
    the import lives inside the probe and its absence is the answer rather than an
    exception. Nothing is estimated: no device means `available: false`, and the
    memory figures appear only when torch can actually report them.
    """
    try:
        import torch
    except Exception as exc:  # ImportError, or a broken install that fails to load
        return {"available": False, "reason": f"torch unavailable: {type(exc).__name__}"}
    if not torch.cuda.is_available():
        return {
            "available": False,
            "torch_version": torch.__version__,
            "reason": "no CUDA device visible to torch",
            "device_count": torch.cuda.device_count(),
        }
    index = torch.cuda.current_device()
    info: dict[str, Any] = {
        "available": True,
        "torch_version": torch.__version__,
        "cuda_version": getattr(torch.version, "cuda", None),
        "device": torch.cuda.get_device_name(index),
        "device_count": torch.cuda.device_count(),
    }
    try:
        free, total = torch.cuda.mem_get_info(index)
        info["memory_total_mib"] = round(total / 1048576)
        info["memory_free_mib"] = round(free / 1048576)
    except Exception:  # older drivers refuse mem_get_info; the name is still useful
        pass
    return info


def _camera_states(ctx: AppContext) -> list[CameraLiveResponse]:
    """One entry per camera this process started, running or not."""
    if ctx.manager is None:
        return []
    states: list[CameraLiveResponse] = []
    for status in ctx.manager.statuses():
        states.append(
            CameraLiveResponse(
                camera_id=status["camera_id"],
                streaming=bool(status["running"]),
                frames_available=ctx.frames.view(status["camera_id"]) is not None,
                role=status["role"],
                synthetic_replay=bool(status["synthetic_replay"]),
                read_plates=bool(status["read_plates"]),
                loop=bool(status["loop"]),
                passes=int(status["passes"]),
                frames_processed=status["frames_processed"],
                error=status["error"],
            )
        )
    return states


@router.get("/health", response_model=HealthResponse)
def health(
    request: Request,
    user: Annotated[CurrentUser, Depends(deps.require_roles(*deps.ANY_ROLE))],
) -> HealthResponse:
    ctx: AppContext = ctx_of(request)
    check = ctx.database.check(MIGRATED_TABLES)
    missing = list(check.get("missing_tables") or [])
    if not check.get("reachable"):
        database = f"unreachable: {check.get('error', 'unknown error')}"
    elif missing:
        database = f"migrations pending: {', '.join(missing)}"
    else:
        database = "ok"

    cameras = _camera_states(ctx)
    degraded = database != "ok" or any(state.error for state in cameras)
    if ctx.manager is not None and not any(
        state.streaming or state.frames_available for state in cameras
    ):
        degraded = True

    return HealthResponse(
        status="degraded" if degraded else "ok",
        database=database,
        anpr_enabled=ctx.cfg.anpr.enabled,
        gpu=gpu_info(),
        cameras=cameras,
        ws_clients=ctx.hub.clients,
        frames_in=ctx.frames.frames_in,
        events_out=ctx.hub.events_in,
        # Both halves of the journey: events the bus dropped before a consumer saw
        # them, and events a viewer was too slow to read. Same symptom downstream,
        # different culprit, so they add into one number a dashboard can alert on
        # and `/health` is the only place that breaks them out.
        dropped_events=ctx.hub.dropped + int(getattr(ctx.bus, "dropped", 0)),
        ws_ticket_refusals=ctx.tickets.refused,
        uptime_s=round(time.time() - ctx.started_at, 3),
    )
