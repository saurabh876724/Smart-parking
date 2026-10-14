"""The FastAPI application: what gets built, what gets started, what gets caught.

`create_app` takes an `AppConfig` and a `Database` and opens nothing itself. That
is the project's rule - a module receives a sub-config, it does not read YAML - and
here it has a second payoff: a test can build an app over an in-memory database in
one line, which is the only reason the auth and endpoint tests below exist at all.

**What the lifespan owns.** The frame hub, the WebSocket subscription, the
optional persistence worker and the camera threads all start on startup and stop
on shutdown, in the reverse order. They are on the lifespan rather than at import
time because uvicorn imports the app module once and runs the lifespan per worker:
pipelines built at import would be started in a process that may never serve a
request, and no camera thread would ever be joined when the server stops.

**Order matters at startup.** The WebSocket hub subscribes to the bus before any
camera publishes, and the persistence worker starts before the cameras do, so the
first frame of a run cannot be missed by either consumer. Shutdown runs the other
way: cameras are stopped and joined first, then the worker drains what is left on
the bus - the same rule `scripts/run_gate.py --persist` follows.

**Nothing here is a fallback.** No default signing key, no created-if-missing
schema, no swallowed database error. `jwt_secret_for` raises before an app exists,
`Database.check` reports an unmigrated database through `/health`, and an
`IntegrityError` becomes a 409 with the driver's text kept in the log rather than
sent to the caller.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from smart_parking import __version__
from smart_parking.api.deps import ApiError, AppContext
from smart_parking.api.live import CameraManager, FrameHub, WebSocketHub
from smart_parking.api.routers import REST_ROUTERS, WEBSOCKET_ROUTERS
from smart_parking.api.security import CredentialError, PasswordService, TokenIssuer
from smart_parking.api.throttle import LoginThrottle
from smart_parking.api.tickets import TicketStore
from smart_parking.core.config import AppConfig, jwt_secret_for
from smart_parking.core.events import EventBus, InMemoryEventBus
from smart_parking.core.logging import get_logger
from smart_parking.db.session import Database
from smart_parking.services.persistence import PersistenceWorker, plate_normaliser_for

log = get_logger("api.app")

DESCRIPTION = """\
Backend for the AI-based smart parking system: JWT-authenticated reads over the
persisted sessions, plates and bays, an append-only audit trail, and the live
surface (multipart MJPEG, JSON overlays and a multiplexed WebSocket) fed by the
same pipelines the Phase 1-6 CLIs run.

**Every frame served here comes from a file on this machine.** `api.pipeline`
names the clips - one real lot recording and one rendered gate fixture replayed
through labelled boxes - and neither is a field camera. Nothing in this build has
been pointed at an RTSP source, so no number in a stream says anything about
latency or frame rate on real hardware.
"""


def _error(
    status_code: int, code: str, message: str, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}, headers=headers
    )


def create_app(
    cfg: AppConfig,
    database: Database | None = None,
    *,
    bus: EventBus | None = None,
    secret: str | None = None,
    persist: bool = False,
    start_pipelines: bool | None = None,
    normalise: Callable[[str], str] | None = None,
) -> FastAPI:
    """Build the application. Raises `SecretUnavailable` with no usable JWT key."""
    database = database or Database.from_config(cfg)
    # Resolved here rather than inside the lifespan so a deployment without a key
    # fails at once, on the thread that started it, instead of half-way through a
    # request that would have had no way to say what went wrong.
    secret = jwt_secret_for(cfg) if secret is None else secret
    # `bus if bus is not None else ...`, not `bus or ...`: an empty bus is falsy
    # through `__len__`, and a caller that passed one would otherwise get an app
    # publishing somewhere else than the object it is holding (see the same note in
    # pipeline/lot_pipeline.py).
    bus = bus if bus is not None else InMemoryEventBus()
    normalise = normalise or plate_normaliser_for(cfg)
    # `start_pipelines=None` defers to the config, which is what a real serve run
    # wants; a test passes False to keep YOLO out of the process entirely.
    stream_cameras = cfg.api.pipeline.enabled if start_pipelines is None else start_pipelines

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        frames = FrameHub(cfg.api.mjpeg)
        hub = WebSocketHub(
            bus,
            cfg.api,
            include_summary=cfg.api.pipeline.send_summary_over_ws,
        )
        ctx = AppContext(
            cfg=cfg,
            database=database,
            bus=bus,
            frames=frames,
            hub=hub,
            passwords=PasswordService(cfg.api.password),
            tokens=TokenIssuer(cfg.api, secret),
            normalise=normalise,
            tickets=TicketStore(cfg.api.ws_ticket_ttl_s),
            login_throttle=LoginThrottle(
                window_s=cfg.api.login_throttle_window_s,
                max_failures=cfg.api.login_throttle_max_failures,
            ),
            started_at=time.time(),
        )
        app.state.ctx = ctx

        worker: PersistenceWorker | None = None
        if persist:
            # start() also seeds cameras and bays from the config, which the
            # session rows' foreign keys need before the first event lands.
            # The worker stays off `AppContext`: no route reads it, and an object a
            # handler can reach is one a handler will eventually write through
            # instead of going the sanctioned way - a repository call in a request
            # transaction.
            worker = PersistenceWorker(database, cfg, bus, normalise=normalise)
            worker.start()

        manager: CameraManager | None = None
        if stream_cameras:
            manager = CameraManager(cfg, bus, frames)
            ctx.manager = manager
            started = manager.start()
            if not started:
                log.error(
                    "api.pipeline is enabled but no camera started: /stream.mjpg will "
                    "answer 409 until a source in cameras.yaml is readable"
                )

        _banner(cfg, ctx, persist=persist, started=bool(stream_cameras))
        try:
            yield
        finally:
            # Cameras first, then the writer: stopping the threads is what makes
            # the queue finite, and a worker stopped before draining would leave
            # the last batch of a run unwritten.
            if manager is not None:
                manager.stop_all()
            if worker is not None:
                stats = worker.stop()
                log.info(
                    "persistence shut down: %d event(s) written in %d batch(es), %d failure(s), "
                    "%d dropped by the bus",
                    stats["events_written"],
                    stats["batches"],
                    stats["failures"],
                    stats["dropped_by_bus"],
                )
                for etype, message in worker.failures[:5]:
                    log.error("could not persist %s: %s", etype, message)

    app = FastAPI(
        title="AI-Based Smart Parking Management System",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        # The API is the product an examiner reads; a browsable contract that
        # matches the doc's section E is worth more than a hidden one.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    if cfg.api.cors.allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.api.cors.allow_origins,
            # "*" rather than a list: no endpoint here answers with anything a
            # cross-origin read should want, and a header that names every route
            # invites a dashboard to add one it never intended to expose.
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=cfg.api.cors.allow_credentials,
        )

    for router in REST_ROUTERS:
        app.include_router(router, prefix=cfg.api.prefix)
    for router in WEBSOCKET_ROUTERS:
        app.include_router(router)

    register_handlers(app)
    return app


def register_handlers(app: FastAPI) -> None:
    """The one place an exception becomes a response body.

    Every handler below produces the same shape - `{"error": {"code", "message"}}`
    - because a client that has to parse two error formats ends up checking the
    status code and throwing the rest away, which is how a `token_expired` answer
    turns into a logout loop.
    """

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        # `exc.headers` is how a 429 gets its `Retry-After` out: a body-only answer
        # tells a client to wait and gives it no number to wait for.
        return _error(exc.status_code, exc.code, exc.message, headers=exc.headers or None)

    @app.exception_handler(CredentialError)
    async def _credential_handler(request: Request, exc: CredentialError) -> JSONResponse:
        # Raised by `security.py` from a path a dependency did not already wrap:
        # the code is the client's, the sentence is safe to show because it names
        # the token's problem, not the account behind it.
        return _error(401, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for error in exc.errors()[:10]:
            where = [str(part) for part in error.get("loc", ()) if part != "body"]
            problems.append(f"{'.'.join(where) or 'request'}: {error.get('msg', 'invalid')}")
        # The field paths and pydantic's sentences are useful to whoever wrote the
        # request; the values are not, because a 422 echo of a rejected plate or
        # password would put the input back on a wire that just refused it.
        return _error(422, "request_invalid", "; ".join(problems) or "the request body is not valid")

    @app.exception_handler(IntegrityError)
    async def _integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        # A database constraint that fired mid-request is a conflict the caller can
        # resolve (retry against the current state, or stop double-submitting), not
        # a server fault. The driver's text - which on Postgres quotes the offending
        # value - stays in the log, where it belongs.
        log.warning("rejected %s %s: %s", request.method, request.url.path, exc)
        return _error(
            409,
            "conflict",
            "this write breaks a database constraint: a duplicate key, or a value "
            "outside what the column is allowed to hold",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _routing_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # The router raises this itself for an unknown path and a wrong verb, and
        # its answer is `{"detail": "Not Found"}` - a second error shape for the
        # one status a client has already learned to read `error.code` from.
        if exc.status_code == 404:
            return _error(404, "route_unknown", f"no {request.method} {request.url.path}")
        code = {405: "method_not_allowed", 401: "token_missing", 403: "permission_denied"}.get(
            exc.status_code, "request_failed"
        )
        return _error(exc.status_code, code, str(exc.detail))


def _banner(cfg: AppConfig, ctx: AppContext, *, persist: bool, started: bool) -> None:
    """One block that says what this process is doing, in plain terms.

    The line that matters most is the one a reader might have hoped was untrue: if
    the streams come from files, the banner has to be the part of the output that
    cannot be missed. A demo shown as a deployment is the failure mode this project
    has been careful about since Phase 5.
    """
    cameras = ctx.manager.statuses() if ctx.manager is not None else []
    synthetic = [c["camera_id"] for c in cameras if c["synthetic_replay"]]
    log.info("=" * 78)
    log.info(
        "smart-parking API v%s - %s%s",
        __version__,
        cfg.api.prefix,
        "" if persist else "  (read-only: events are NOT being persisted)",
    )
    log.info("database: %s", ctx.database.url)
    if started:
        log.info(
            "streams: %d camera thread(s) - %s",
            len(cameras),
            ", ".join(c["camera_id"] for c in cameras) or "none started",
        )
        log.info(
            "WARNING: these are FILE REPLAYS, not field cameras. Sources come from "
            "api.pipeline in configs/app.yaml; %s is rendered fixture footage with "
            "labelled boxes, and no latency or FPS figure here describes a real camera.",
            ", ".join(synthetic) if synthetic else "no camera",
        )
    else:
        log.info("streams: none (api.pipeline disabled or start_pipelines=False)")
    reading = [c["camera_id"] for c in cameras if c["read_plates"]]
    if not cfg.anpr.enabled:
        log.info("ANPR: disabled in configs/app.yaml")
    elif reading:
        # Said out loud because a served plate is a plate a browser has now
        # copied: the picture carries the number, whatever the API masks.
        log.info("ANPR: READING PLATES ON the served stream(s) %s", ", ".join(reading))
    else:
        log.info("ANPR: configured, off on every served stream (read_plates: false)")
    log.info("=" * 78)
