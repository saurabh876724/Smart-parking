"""Login, token refresh and the current user (doc section E, "Auth")."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from smart_parking.api import deps
from smart_parking.api.deps import ApiError, AppContext, CurrentUser, UserDep, ctx_of
from smart_parking.api.schemas import (
    LoginRequest,
    LogoutResponse,
    MeResponse,
    RefreshRequest,
    TokenPairResponse,
    UserCreateRequest,
    UserResponse,
    WsTicketResponse,
    iso,
)
from smart_parking.api.security import REFRESH, CredentialError
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import Role
from smart_parking.db.base import utcnow

log = get_logger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])
AdminDep = Annotated[CurrentUser, Depends(deps.require_roles(Role.ADMIN))]

_NOT_ALLOWED = "email or password is not an active account"


def _pair(ctx: AppContext, user_id: int, email: str, role: Role) -> TokenPairResponse:
    access, refresh, expires_at = ctx.tokens.issue_pair(user_id=user_id, email=email, role=role)
    return TokenPairResponse(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=expires_at.isoformat(),
        role=role,
        email=email,
    )


@router.post("/login", response_model=TokenPairResponse)
def login(
    body: LoginRequest,
    request: Request,
    repos: deps.ReposDep,
) -> TokenPairResponse:
    """Password in, token pair out - the only endpoint without a bearer token.

    One answer for "no such account", "wrong password" and "deactivated": the
    three are a user-enumeration oracle, and an operator who mistyped their own
    address learns nothing useful from the difference. The audit row keeps the
    distinction, which is where it is useful.

    A fourth answer exists before this one is reached: `429` once
    `api.login_throttle_max_failures` wrong guesses have come from the same
    address for the same address in `api.login_throttle_window_s`. It is a
    different status rather than the same 401 because the client has to stop, and
    because reading the stored hash at all is the cost this refuses to pay.
    """
    ctx = ctx_of(request)
    ip = deps.client_ip(request)
    key = (body.email, ip or "")
    wait = ctx.login_throttle.retry_after(key)
    if wait is not None:
        # No audit row: the failures that earned the wait are already on the trail,
        # and a refusal the attacker can repeat forever would grow an append-only
        # table faster than the guesses it is stopping.
        log.info(
            "throttled sign-in for %s from %s: %d failure(s) in %ds, retry in %ds",
            body.email,
            ip,
            ctx.login_throttle.failures(key),
            ctx.login_throttle.window_s,
            wait,
        )
        raise ApiError(
            429,
            "too_many_attempts",
            f"too many failed sign-ins for this address; try again in {wait} seconds",
            headers={"Retry-After": str(wait)},
        )

    row = repos.users.by_email(body.email)
    reason = "unknown_email"
    if row is None:
        verified = False
    elif not row.is_active:
        verified, reason = False, "inactive"
    else:
        try:
            verified = ctx.passwords.verify(row.password_hash, body.password)
            reason = "bad_password"
        except CredentialError:
            # A stored hash this process cannot read is a broken row, not a weak
            # password. The caller still gets the wrong-password answer.
            verified, reason = False, "hash_unreadable"

    if not verified:
        ctx.login_throttle.record_failure(key)
        deps.audit(
            ctx,
            action="login_failed",
            actor_email=body.email,
            detail={"reason": reason},
            source_ip=ip,
        )
        raise ApiError(401, "bad_credentials", _NOT_ALLOWED)

    ctx.login_throttle.record_success(key)
    if ctx.passwords.needs_rehash(row.password_hash):
        # The only moment the plaintext is available, so this is when a hash made
        # at older cost parameters gets upgraded.
        row.password_hash = ctx.passwords.hash(body.password, email=row.email)
    repos.audit.append("login", actor=row, detail={"role": row.role.value}, source_ip=ip)
    return _pair(ctx, row.id, row.email, row.role)


@router.post("/logout", response_model=LogoutResponse)
def logout(request: Request, user: UserDep, repos: deps.ReposDep) -> LogoutResponse:
    """Withdraw every credential this account holds, then answer with the cutoff.

    There is no session to delete: the tokens are signed, not stored, so the only
    server-side meaning of "log out" is a timestamp the account's credentials must
    be older than (`users.tokens_invalid_before`, compared against each token's
    `iat` by `deps.reject_revoked`). That kills the access token in the caller's
    browser **and** the refresh token it kept, on any device, until the next
    sign-in - which is what an operator who walked away from a display wall needs,
    and what dropping the tokens client-side alone would only look like.

    The row is deactivated-not-deleted territory: the audit entry stays, and the
    account can sign in again with its password.
    """
    ctx = ctx_of(request)
    row = repos.users.invalidate_tokens(user.user_id, utcnow())
    if row is None:
        # `UserDep` just read this row, so this is a deletion that happened inside
        # the request's own transaction - accounts are never deleted, which is why
        # it is a 404 rather than a branch worth defending twice.
        raise ApiError(404, "user_unknown", "this token's account is not in the database")
    repos.audit.append(
        "logout",
        actor=deps.actor(repos, user),
        target_type="user",
        target_id=str(row.id),
        detail={"role": row.role.value},
        source_ip=deps.client_ip(request),
    )
    return LogoutResponse(revoked_at=iso(row.tokens_invalid_before), email=row.email)


@router.post("/ws-ticket", response_model=WsTicketResponse)
def ws_ticket(request: Request, user: UserDep) -> WsTicketResponse:
    """A one-time ticket for `WS /ws/live` and the overlay socket.

    The handshake needs a credential in the URL because a browser's `WebSocket`
    cannot set a header, and a URL outlives the request that used it: nginx log,
    proxy log, browser history, a crash report. So what travels there is minted
    for exactly that (`api/tickets.py`) and nothing else - not the fifteen-minute
    access token, which was a live credential wherever the socket URL got copied.

    It is issued to a bearer-authenticated caller, so a refresh token cannot buy
    one: `UserDep` decodes it as an access token and refuses it by name.
    """
    ctx = ctx_of(request)
    ticket, expires_at = ctx.tickets.issue(user_id=user.user_id, email=user.email, role=user.role)
    return WsTicketResponse(ticket=ticket, expires_at=expires_at.isoformat())


@router.post("/refresh", response_model=TokenPairResponse)
def refresh(body: RefreshRequest, request: Request, repos: deps.ReposDep) -> TokenPairResponse:
    """A refresh token buys a new pair, and can call nothing else.

    Rotation rather than a new access token alone: a refresh credential that stays
    valid after being used is a credential that lives for twelve hours wherever it
    was copied.
    """
    ctx = ctx_of(request)
    try:
        claims = ctx.tokens.decode(body.refresh_token, expect=REFRESH)
    except CredentialError as exc:
        raise ApiError(401, exc.code, str(exc)) from exc
    row = repos.users.get(claims.user_id)
    if row is None or not row.is_active:
        raise ApiError(401, "user_inactive", "this account is not active any more")
    # The same cutoff a logout writes: without it, signing out of a dashboard left
    # the refresh token in the browser able to mint a fresh pair, which is a logout
    # that un-logs itself in.
    deps.reject_revoked(row, claims.issued_at)
    if row.role is not claims.role:
        raise ApiError(403, "role_changed", "this token's role no longer matches the account")
    return _pair(ctx, row.id, row.email, row.role)


def _me(user: CurrentUser, repos: deps.ReposDep) -> MeResponse:
    row = repos.users.get(user.user_id)
    if row is None:
        raise ApiError(404, "user_unknown", "this token's account is not in the database")
    return MeResponse(reveal_plates=user.can_reveal_plates, **UserResponse.from_row(row).model_dump())


@router.get("/me", response_model=MeResponse)
def me(user: UserDep, repos: deps.ReposDep) -> MeResponse:
    """The doc's `GET /users/me`, also mounted at `/auth/me`.

    Two paths, one function: the doc's Auth table lists it here, and a dashboard
    asking "who am I" should not have to know which table the answer came from.
    """
    return _me(user, repos)


users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/me", response_model=MeResponse)
def users_me(user: UserDep, repos: deps.ReposDep) -> MeResponse:
    return _me(user, repos)


@users_router.get("", response_model=list[UserResponse])
def list_users(user: AdminDep, repos: deps.ReposDep) -> list[UserResponse]:
    return [UserResponse.from_row(row) for row in repos.users.all()]


@users_router.post("", response_model=UserResponse, status_code=201)
def create_user(
    body: UserCreateRequest,
    request: Request,
    user: AdminDep,
    repos: deps.ReposDep,
) -> UserResponse:
    """Create an account. ADMIN only, audited as `user_created`.

    The password never leaves this function: it is checked by the policy, hashed,
    and only the hash reaches the row. The audit detail carries the address and
    the role - never the secret, never the hash.
    """
    ctx = ctx_of(request)
    if repos.users.by_email(body.email) is not None:
        raise ApiError(409, "email_taken", "an account with this email already exists")
    try:
        password_hash = ctx.passwords.hash(body.password, email=body.email)
    except CredentialError as exc:
        raise ApiError(422, exc.code, str(exc)) from exc
    row = repos.users.create(body.email, password_hash, body.role)
    repos.audit.append(
        "user_created",
        actor=deps.actor(repos, user),
        target_type="user",
        target_id=str(row.id),
        detail={"email": row.email, "role": row.role.value},
        source_ip=deps.client_ip(request),
    )
    return UserResponse.from_row(row)


@users_router.post("/{user_id}/deactivate", response_model=UserResponse)
def deactivate_user(
    user_id: int,
    request: Request,
    user: AdminDep,
    repos: deps.ReposDep,
) -> UserResponse:
    """Deactivate, never delete: the visits an operator booked outlive the account."""
    row = repos.users.get(user_id)
    if row is None:
        raise ApiError(404, "user_unknown", f"no account {user_id}")
    if row.role is Role.ADMIN and row.is_active and repos.users.count_active_admins() <= 1:
        # Every other endpoint needs a token and the only way to mint an account is
        # an ADMIN call, so the last live ADMIN is the difference between a locked
        # door and a system with no door. Recovery from that is a database edit.
        raise ApiError(409, "last_admin", "this is the only active ADMIN account")
    row = repos.users.deactivate(user_id)
    repos.audit.append(
        "user_deactivated",
        actor=deps.actor(repos, user),
        target_type="user",
        target_id=str(row.id),
        detail={"email": row.email},
        source_ip=deps.client_ip(request),
    )
    return UserResponse.from_row(row)
