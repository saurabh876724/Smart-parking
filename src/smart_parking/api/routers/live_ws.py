"""The two WebSocket endpoints: the event channel and the per-camera overlay.

Both are read-only. A dashboard that sends nothing needs no command protocol, and
every message that leaves here is either an event the bus already published or a
frame the pipeline already drew - there is no client input to validate, which is
one fewer place a bug can hide.

Authentication is a query parameter because a browser's `WebSocket` cannot set an
`Authorization` header - but the parameter is a one-time ticket from
`POST /auth/ws-ticket`, not the access token. Whatever is in a URL is copied into
nginx's access log, a proxy's, the browser's history and any report that quotes a
failed connect, so the thing that travels there has to be worthless by then: opaque,
spent on first use, and seconds old. See `api/tickets.py`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from smart_parking.api import deps
from smart_parking.api.deps import ApiError, AppContext, CurrentUser
from smart_parking.api.security import mask_label

router = APIRouter(tags=["live"])

# 4001-4999 are reserved for application use; these two say why the socket closed
# without the client having to read a log.
CLOSE_UNAUTHORISED = 4401
CLOSE_NO_CAMERA = 4404
CLOSE_ROLE = 4403


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """Slot, summary, session, violation and camera events on one channel.

    A slow client is dropped by the hub rather than slowed down by it: the queue
    is bounded by `api.ws_queue_size`, and when it fills the oldest unread event
    stays unread and the counter goes up. Inference never waits for a browser.
    """
    user = await _authenticate(websocket)
    if user is None:
        return
    ctx: AppContext = websocket.app.state.ctx
    await websocket.accept()
    sub = ctx.hub.connect(user.role, loop=asyncio.get_running_loop())
    await websocket.send_json(
        {
            "type": "hello",
            "channel": "session",
            "camera_id": None,
            "ts": time.time(),
            "payload": {
                "role": user.role.value,
                "reveal_plates": sub.reveal_plates,
                "queue_size": ctx.cfg.api.ws_queue_size,
            },
        }
    )
    try:
        while True:
            payload = await sub.queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    except (RuntimeError, OSError):
        # The socket died mid-send. The alternative is an exception on the
        # event loop for every viewer that closes a tab.
        pass
    finally:
        ctx.hub.disconnect(sub)


@router.websocket("/ws/cameras/{camera_id}/overlay")
async def ws_overlay(websocket: WebSocket, camera_id: str) -> None:
    """Boxes and labels for one camera, for a canvas drawn over the MJPEG picture.

    The frame's sequence number is the cursor: a viewer that falls behind gets the
    newest frame and skips the ones it missed, which is the right trade for an
    overlay (a box for a car that left two seconds ago is worse than no box).
    """
    user = await _authenticate(websocket)
    if user is None:
        return
    ctx: AppContext = websocket.app.state.ctx
    await websocket.accept()
    if ctx.frames.view(camera_id) is None:
        await websocket.send_json({"type": "error", "code": "no_frame", "camera_id": camera_id})
        await websocket.close(code=CLOSE_NO_CAMERA, reason="no_frame")
        return
    interval = 1.0 / max(0.5, ctx.cfg.api.mjpeg.fps)
    seq: int | None = None
    try:
        while True:
            view = ctx.frames.view(camera_id)
            if view is None or view.seq == seq:
                await asyncio.sleep(interval)
                continue
            seq = view.seq
            await websocket.send_json(_overlay(view, user))
    except WebSocketDisconnect:
        pass
    except (RuntimeError, OSError):
        pass


async def _authenticate(websocket: WebSocket) -> CurrentUser | None:
    """Return the caller, or None once the handshake has been refused.

    The close happens before `accept()`, which Starlette turns into a rejected
    handshake - a browser sees `onclose` with the code and nothing else. Raising
    here instead would surface as an unhandled exception in the server log.

    A `?token=` on this URL is refused **by name** rather than treated as "no
    credential presented". A dashboard cached from before tickets would otherwise
    get `token_missing` and log its operator out, when the finding is that the
    socket stopped taking bearer tokens in a URL and the bundle needs to fetch a
    ticket instead.
    """
    params = websocket.query_params
    if "token" in params:
        return await _refuse(
            websocket,
            ApiError(
                401,
                "ticket_required",
                "this socket takes ?ticket=<one-time ticket>, not a bearer token in the URL",
            ),
        )
    try:
        return deps.ws_authenticate(websocket.app, params.get("ticket", ""))
    except ApiError as exc:
        return await _refuse(websocket, exc)


async def _refuse(websocket: WebSocket, exc: ApiError) -> CurrentUser | None:
    """Close a handshake that never became a connection, and say why by code."""
    code = CLOSE_UNAUTHORISED if exc.status_code == 401 else CLOSE_ROLE
    await websocket.close(code=code, reason=exc.code)
    return None


def _overlay(view, user: CurrentUser) -> dict[str, Any]:
    payload = view.overlay()
    if not user.can_reveal_plates:
        for track in payload["tracks"]:
            track["label"] = mask_label(track["label"])
    return payload
