"""Request wiring: the shared context, the session-per-request, and the token.

`AppContext` is built once, by the application's lifespan, and hangs off
`app.state`. Every dependency in this module reads from it, which is what keeps
the rest of `api/` free of `configs/app.yaml`: the rule the whole project follows
- modules receive a Pydantic sub-config, they do not open files - applies to the
HTTP layer as much as to the AI layers.

Three choices in here are worth their reasons:

* **A token is checked against the database on every request.** Accounts are
  deactivated, never deleted, so an access token that still has ten minutes to
  live must not outlive the person it belongs to.
* **One `ApiError` type, one handler.** The client reads `error.code`, the human
  reads `error.message`. Raising HTTPException per route would scatter the
  status/wording pairs across fifteen places instead of one table.
* **One transaction per request.** The generator dependency closes after the
  response, so a route that wrote three rows in one call has them committed - or
  none, if it failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Callable, Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from smart_parking.api.live import CameraManager, FrameHub, WebSocketHub
from smart_parking.api.security import ACCESS, PLATE_REVEAL_ROLES, CredentialError, PasswordService, TokenIssuer
from smart_parking.api.throttle import LoginThrottle
from smart_parking.api.tickets import TicketStore
from smart_parking.core.config import AppConfig
from smart_parking.core.events import EventBus
from smart_parking.core.schemas import Role
from smart_parking.db.models import User
from smart_parking.db.repositories import Repositories
from smart_parking.db.session import Database

# Who may do what. A table rather than fifteen decorators, because a permission
# rule expressed twice is a rule that can drift.
WRITE_ROLES = (Role.ADMIN, Role.OPERATOR)
ADMIN_ONLY = (Role.ADMIN,)
ANY_ROLE = (Role.ADMIN, Role.OPERATOR, Role.VIEWER, Role.CUSTOMER)


class ApiError(Exception):
    """An error the client is meant to read: status, machine code, sentence.

    `headers` exists for the one answer a client cannot act on from the body
    alone: a 429 needs `Retry-After`, or "wait" becomes "retry as fast as you
    can" and the throttle that refused the attempt also caused the next one.
    """

    def __init__(
        self, status_code: int, code: str, message: str, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})

    def to_body(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass
class AppContext:
    """Everything the routes need, assembled by `create_app`.

    `tickets` and `login_throttle` are per process, which is what their docstrings
    say they are: a socket ticket is redeemed by the worker that issued it, and a
    deployment that runs several workers either pins a dashboard to one or moves
    both rules to a shared store.
    """

    cfg: AppConfig
    database: Database
    bus: EventBus
    frames: FrameHub
    hub: WebSocketHub
    passwords: PasswordService
    tokens: TokenIssuer
    normalise: Callable[[str], str]
    tickets: TicketStore
    login_throttle: LoginThrottle
    # None when the server is running without streaming cameras, which is a
    # legitimate deployment (a DB-only box) rather than a failure.
    manager: CameraManager | None = None
    started_at: float = 0.0


@dataclass(frozen=True)
class CurrentUser:
    """The caller, as far as this request is concerned."""

    user_id: int
    email: str
    role: Role

    @property
    def can_reveal_plates(self) -> bool:
        return self.role in PLATE_REVEAL_ROLES

    def may(self, *roles: Role) -> bool:
        return self.role in roles


bearer = HTTPBearer(auto_error=False)


def ctx_of(request: Request) -> AppContext:
    return request.app.state.ctx


def authenticate(ctx: AppContext, token: str) -> CurrentUser:
    """Decode an access token and confirm the account behind it is still live."""
    claims = ctx.tokens.decode(token, expect=ACCESS)
    return verify_account(
        ctx,
        user_id=claims.user_id,
        role=claims.role,
        issued_at=claims.issued_at,
    )


def reject_revoked(row: User, issued_at: datetime) -> None:
    """Refuse a credential the account has since signed out.

    The cutoff is compared in naive UTC: the column is naive because SQLite stores
    no offset (`db/base.py`), while a JWT's `iat` is an epoch integer read back as
    an aware datetime (`security.py::TokenClaims`). One `replace` on the token's
    side puts both on the clock the row lives on.

    The cutoff is the sign-out rounded **up** to the next whole second, which is
    what makes `issued_at < cutoff` enough: a token minted in the same second as
    the logout dies too, and the worst that costs is one extra sign-in.
    """
    cutoff = row.tokens_invalid_before
    if cutoff is None:
        return
    if issued_at.replace(tzinfo=None) < cutoff:
        raise ApiError(401, "token_revoked", "this credential was signed out; sign in again")


def verify_account(ctx: AppContext, *, user_id: int, role: Role, issued_at: datetime) -> CurrentUser:
    """The account behind a credential, as it is **now** rather than as claimed.

    Three ways this refuses, and they are the same three for a Bearer token, a
    refresh and a socket ticket: an account that is gone, one that has been
    switched off, and one whose role has moved out from under the credential. A
    fourth - `token_revoked` - is the logout cutoff.

    This is why the row is read on every request rather than trusting the claims:
    a token's whole design is that nobody checks with the server that minted it,
    and an account that can be deactivated at 14:00 has to stop working at 14:00.
    """
    with ctx.database.scope() as session:
        row = Repositories(session, normalise=ctx.normalise).users.get(user_id)
    if row is None:
        raise ApiError(401, "user_unknown", "the account this token was issued to no longer exists")
    if not row.is_active:
        raise ApiError(401, "user_inactive", "this account has been deactivated")
    reject_revoked(row, issued_at)
    if row.role is not role:
        # The token still says what the role used to be. Trust the row: a demoted
        # operator must not keep admin powers until the token expires.
        raise ApiError(403, "role_changed", "this token's role no longer matches the account")
    return CurrentUser(user_id=row.id, email=row.email, role=row.role)


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> CurrentUser:
    if credentials is None:
        raise ApiError(401, "token_missing", "an Authorization: Bearer <access token> header is required")
    try:
        return authenticate(ctx_of(request), credentials.credentials)
    except CredentialError as exc:
        raise ApiError(401, exc.code, str(exc)) from exc


def require_roles(*roles: Role):
    """A dependency that admits only the listed roles."""

    def dependency(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
        if user.role not in roles:
            raise ApiError(
                403,
                "role_not_allowed",
                f"this endpoint needs {'/'.join(r.value for r in roles)}, not {user.role.value}",
            )
        return user

    return dependency


def ws_authenticate(app, ticket: str) -> CurrentUser:
    """Open a socket with a one-time ticket, never with a bearer token.

    A WebSocket handshake cannot set an `Authorization` header, so its credential
    is in the URL - and a URL is logged by nginx, kept in browser history and
    quoted by whatever error report catches a failed connect. The old shape of
    this call took the access token, which made every one of those copies a live
    credential for `api.access_token_ttl_s`. A `api/tickets.py` grant is opaque,
    spent on first use, and expires in seconds.

    The account is re-read on redemption, not trusted from the ticket: a sign-out
    or a deactivation between the handshake request and the connect attempt is
    exactly the window a socket would otherwise slip through.
    """
    if not ticket:
        raise ApiError(
            401,
            "token_missing",
            "connect with ?ticket=<one-time ticket from POST /auth/ws-ticket>",
        )
    ctx: AppContext = app.state.ctx
    grant = ctx.tickets.redeem(ticket)
    if grant is None:
        raise ApiError(401, "token_invalid", "this socket ticket is unknown, already used, or expired")
    return verify_account(ctx, user_id=grant.user_id, role=grant.role, issued_at=grant.issued_at)


def viewer_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> CurrentUser:
    """The camera endpoints' dependency: header **or** `?token=`.

    An `<img src>` cannot set an Authorization header, which is why the query form
    is allowed here and nowhere else. It is tolerable for two specific reasons:
    the credential is the short-lived access token, so a stream URL that survives
    in a log or a history entry is worth nothing after
    `api.access_token_ttl_s`; and `deploy/nginx.conf` deliberately logs requests
    without their query strings, so the proxy this runs behind does not keep the
    URL at all.

    A WebSocket has neither excuse and takes a single-use ticket instead - see
    `ws_authenticate`. The picture is fetched once per URL and a ticket is spent
    per connection, which the browser's image cache makes the wrong tool here.
    """
    token = credentials.credentials if credentials is not None else request.query_params.get("token", "")
    if not token:
        raise ApiError(
            401,
            "token_missing",
            "send an Authorization header or a ?token= query parameter",
        )
    try:
        return authenticate(ctx_of(request), token)
    except CredentialError as exc:
        raise ApiError(401, exc.code, str(exc)) from exc


def repositories(request: Request) -> Iterator[Repositories]:
    """One session per request; `Database.scope` commits when the route returns."""
    ctx = ctx_of(request)
    with ctx.database.scope() as session:
        yield Repositories(session, normalise=ctx.normalise)


ReposDep = Annotated[Repositories, Depends(repositories)]
UserDep = Annotated[CurrentUser, Depends(current_user)]
ViewerDep = Annotated[CurrentUser, Depends(viewer_user)]


def actor(repos: Repositories, user: CurrentUser):
    """The caller's ORM row, so an audit row carries the FK the table wants.

    The row has to come from the same session as the write: `AuditLogRepository`
    reads `actor.id`, and a detached row would be a lazy load on a closed session.
    """
    return repos.users.get(user.user_id)


def client_ip(request: Request) -> str | None:
    """The address to put in the audit row.

    `X-Forwarded-For` is deliberately not trusted here: this system runs behind
    no proxy in this project, so the only value worth recording is the one the
    connection actually came from, and a forwarded header an attacker controls
    would make the trail untrustworthy.
    """
    return request.client.host if request.client else None


def audit(
    ctx: AppContext,
    *,
    action: str,
    actor_email: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
    source_ip: str | None = None,
) -> None:
    """Write one audit row in a transaction of its own.

    For the attempts that are about to fail: a refused login rolls the request's
    session back, and the record of the refusal is the reason the table exists.
    Every other audited operation uses `repos.audit` so its row and its change
    commit together.
    """
    with ctx.database.scope() as session:
        Repositories(session, normalise=ctx.normalise).audit.append(
            action,
            actor_email=actor_email,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            source_ip=source_ip,
        )
