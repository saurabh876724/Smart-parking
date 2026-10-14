"""Every router in one import, so `app.py` mounts a list rather than a lecture.

The order is the order `/docs` shows them in, and it is the order a reader of the
architecture doc's section E works through: prove who you are, then the cameras,
then the bays, then the visits, then the numbers, then the trail.

Two lists, because the doc writes its two halves differently. REST lives under the
`Base path /api/v1` its section E opens with; the WebSocket lines are written as
`WS /ws/live?token=`, with no prefix, and a dashboard's `new WebSocket(...)` is
built from a page URL rather than from an API base - so the sockets are mounted at
the root exactly as the doc names them. The parameter is a **ticket** now
(`POST /auth/ws-ticket`, `api/tickets.py`): the doc's shape - a credential in the
query string, at the root, without the prefix - is what the tickets replaced, and
a bearer token presented there is refused by name.

Two rules hold across the set:

* Only `auth` is unauthenticated. Every other router either depends on
  `deps.current_user` (or `viewer_user`, the same check through a query
  parameter, for the picture an `<img>` tag cannot send a header with) or is a
  WebSocket that refuses the handshake without a ticket.
* Nothing here builds a query. A route that needs a row asks a repository for it,
  which is what keeps the SQL in `db/` and reviewable in one place.
"""

from __future__ import annotations

from fastapi import APIRouter

from smart_parking.api.routers import (
    audit,
    auth,
    cameras,
    health,
    live_ws,
    plates,
    sessions,
    slots,
    stats,
)

# Mounted under `api.prefix`.
REST_ROUTERS: list[APIRouter] = [
    auth.router,
    auth.users_router,
    cameras.router,
    slots.router,
    sessions.router,
    plates.router,
    stats.router,
    audit.router,
    health.router,
]

# Mounted at the root, as the doc spells them.
WEBSOCKET_ROUTERS: list[APIRouter] = [live_ws.router]
