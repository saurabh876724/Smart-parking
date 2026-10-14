"""Cameras: configuration, health, and the two ways to watch one (Phase 8)."""

from __future__ import annotations

import time
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from smart_parking.api import deps
from smart_parking.api.deps import ApiError, AppContext, CurrentUser, ctx_of
from smart_parking.api.schemas import (
    CameraLiveResponse,
    CameraRegisterRequest,
    CameraResponse,
    CameraUpdateRequest,
)
from smart_parking.api.security import mask_label
from smart_parking.core.schemas import CameraRole, Role

router = APIRouter(prefix="/cameras", tags=["cameras"])
AdminDep = Annotated[CurrentUser, Depends(deps.require_roles(Role.ADMIN))]
OperatorDep = Annotated[CurrentUser, Depends(deps.require_roles(*deps.WRITE_ROLES))]

# A stream with no frames for this long ends. An endless 200 OK that carries
# nothing is how a dead camera looks alive in a dashboard.
NO_FRAME_TIMEOUT_S = 30.0


def _live(ctx: AppContext, camera_id: str, role: str = "UNKNOWN") -> CameraLiveResponse:
    """What the running processes know about this camera right now."""
    runner = ctx.manager.get(camera_id) if ctx.manager is not None else None
    view = ctx.frames.view(camera_id)
    if runner is None:
        return CameraLiveResponse(
            camera_id=camera_id,
            streaming=False,
            frames_available=view is not None,
            role=role,
        )
    status = runner.status()
    return CameraLiveResponse(
        camera_id=camera_id,
        streaming=bool(status["running"]),
        frames_available=view is not None,
        role=status["role"],
        synthetic_replay=status["synthetic_replay"],
        read_plates=status["read_plates"],
        loop=status["loop"],
        passes=status["passes"],
        frames_processed=status["frames_processed"],
        error=status["error"],
    )


def _row(repos, camera_id: str):
    row = repos.cameras.get(camera_id)
    if row is None:
        raise ApiError(404, "camera_unknown", f"no camera {camera_id!r} is registered")
    return row


@router.get("", response_model=list[dict])
def list_cameras(user: deps.UserDep, repos: deps.ReposDep, request: Request) -> list[dict]:
    """The doc's `GET /cameras`: the row and the live status, per camera.

    Merged because asking for a camera list is asking "which of these can I
    watch"; two calls the dashboard has to join would put the meaning of `status`
    in the client.
    """
    ctx = ctx_of(request)
    out = []
    for row in repos.cameras.all():
        camera = CameraResponse.from_row(row)
        live = _live(ctx, row.camera_id, role=camera.role)
        out.append({**camera.model_dump(), "live": live.model_dump()})
    return out


@router.get("/{camera_id}", response_model=dict)
def get_camera(camera_id: str, user: deps.UserDep, repos: deps.ReposDep, request: Request) -> dict:
    ctx = ctx_of(request)
    camera = CameraResponse.from_row(_row(repos, camera_id))
    live = _live(ctx, camera_id, role=camera.role)
    return {**camera.model_dump(), "live": live.model_dump()}


@router.post("", response_model=CameraResponse, status_code=201)
def register_camera(
    body: CameraRegisterRequest,
    request: Request,
    user: AdminDep,
    repos: deps.ReposDep,
) -> CameraResponse:
    """Register a camera. ADMIN only, audited as `camera_registered`.

    The doc's request shape is `{name, role, rtsp_url, lot_id}`; this takes
    `camera_id` (the primary key every other table points at), `source` instead of
    `rtsp_url` (a source here is a file path or a camera index, and encrypting a
    path is not what `rtsp_url_encrypted` was for) and no `lot_id` because the
    parking-lots table is a later phase - `camera_id` is the lot key until it
    exists.

    Registering does **not** start a stream: a row is configuration, and the
    cameras that run are the ones in `api.pipeline.cameras`, which is a decision
    about this machine's GPU rather than about a database row.
    """
    if repos.cameras.get(body.camera_id) is not None:
        raise ApiError(409, "camera_exists", f"camera {body.camera_id!r} is already registered")
    row = repos.cameras.get_or_create(
        body.camera_id,
        name=body.name,
        role=CameraRole(body.role),
        source=body.source,
        slots_file=body.slots_file,
        gate_file=body.gate_file,
        fps_limit=body.fps_limit,
    )
    repos.audit.append(
        "camera_registered",
        actor=deps.actor(repos, user),
        target_type="camera",
        target_id=row.camera_id,
        # The source is a path, not a credential, so it is worth recording: an
        # operator reviewing this row wants to know what was pointed at.
        detail={"role": row.role.value, "source": row.source},
        source_ip=deps.client_ip(request),
    )
    return CameraResponse.from_row(row)


@router.patch("/{camera_id}", response_model=CameraResponse)
def update_camera(
    camera_id: str,
    body: CameraUpdateRequest,
    request: Request,
    user: OperatorDep,
    repos: deps.ReposDep,
) -> CameraResponse:
    """Change a camera's configuration, audited with only what changed.

    An audit row full of unchanged fields is noise; the point of the trail is to
    show what a person touched.
    """
    row = _row(repos, camera_id)
    changes = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not changes:
        raise ApiError(422, "nothing_to_change", "send at least one field to update")
    repos.cameras.update(row, **changes)
    repos.audit.append(
        "camera_updated",
        actor=deps.actor(repos, user),
        target_type="camera",
        target_id=row.camera_id,
        detail={"changed": sorted(changes)},
        source_ip=deps.client_ip(request),
    )
    return CameraResponse.from_row(row)


@router.post("/{camera_id}/restart", response_model=CameraLiveResponse)
def restart_camera(
    camera_id: str,
    request: Request,
    user: OperatorDep,
    repos: deps.ReposDep,
) -> CameraLiveResponse:
    """End this camera's run and start a fresh pipeline.

    Only a camera the server started itself can be restarted - a row in the
    database is not a process. The runner rebuilds from the current config, so a
    polygon edit made through `/slots` takes effect on the next pass.
    """
    ctx = ctx_of(request)
    if ctx.manager is None:
        raise ApiError(409, "pipelines_disabled", "this server is not running any cameras")
    runner = ctx.manager.get(camera_id)
    if runner is None:
        raise ApiError(409, "not_streaming", f"camera {camera_id!r} is not one this server streams")
    if not runner.restart():
        raise ApiError(409, "restart_failed", f"camera {camera_id!r} would not stop")
    repos.audit.append(
        "camera_restart",
        actor=deps.actor(repos, user),
        target_type="camera",
        target_id=camera_id,
        detail={"passes": runner.passes},
        source_ip=deps.client_ip(request),
    )
    return _live(ctx, camera_id, role=runner.status()["role"])


@router.get("/{camera_id}/snapshot")
def snapshot(camera_id: str, user: deps.ViewerDep, request: Request) -> Response:
    """The newest annotated frame, unscaled - the doc's calibration use case."""
    ctx = ctx_of(request)
    data = ctx.frames.snapshot(camera_id)
    if data is None:
        raise ApiError(409, "no_frame", f"camera {camera_id!r} has not produced a frame yet")
    return Response(content=data, media_type="image/jpeg")


def _multipart(camera_id: str, ctx: AppContext, interval: float) -> Iterator[bytes]:
    """One part per frame, paced per viewer rather than per processed frame.

    The boundary is written by hand because that is the format: a browser will
    render `multipart/x-mixed-replace` from a generator and nothing else, and it
    is why the doc's MVP is MJPEG rather than WebRTC.
    """
    boundary = f"smartparking{camera_id}"
    seq: int | None = None
    quiet_since = time.monotonic()
    while True:
        frame = ctx.frames.jpeg_since(camera_id, seq)
        if frame is None:
            if time.monotonic() - quiet_since > NO_FRAME_TIMEOUT_S:
                return
            # Shorter than the interval so a camera that resumes is picked up
            # without the viewer waiting a whole frame period to notice.
            time.sleep(min(interval, 0.2))
            continue
        quiet_since = time.monotonic()
        seq, jpeg = frame
        yield (
            f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        ).encode("ascii") + jpeg + b"\r\n"
        time.sleep(interval)


@router.get("/{camera_id}/stream.mjpg")
def stream(camera_id: str, user: deps.ViewerDep, request: Request) -> Response:
    """Annotated live feed for a browser `<img>` tag.

    Auth by `?token=` is allowed here (see `deps.viewer_user`) because an image
    element cannot set a header.
    """
    ctx = ctx_of(request)
    if ctx.frames.view(camera_id) is None:
        raise ApiError(409, "no_frame", f"camera {camera_id!r} has not produced a frame yet")
    interval = 1.0 / max(0.5, ctx.cfg.api.mjpeg.fps)
    boundary = f"smartparking{camera_id}"
    return StreamingResponse(
        _multipart(camera_id, ctx, interval),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{camera_id}/overlay", response_model=dict)
def overlay(camera_id: str, user: deps.ViewerDep, request: Request) -> dict:
    """The JSON half of the newest frame, for a canvas drawing its own boxes.

    The doc suggests this as the cheaper alternative to burning boxes into video;
    the polling form of it is what a client that cannot hold a WebSocket uses.
    """
    ctx = ctx_of(request)
    view = ctx.frames.view(camera_id)
    if view is None:
        raise ApiError(409, "no_frame", f"camera {camera_id!r} has not produced a frame yet")
    payload = view.overlay()
    if not user.can_reveal_plates:
        for track in payload["tracks"]:
            track["label"] = mask_label(track["label"])
    return payload
