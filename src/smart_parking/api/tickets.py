"""One-time tickets for the WebSocket handshake.

A browser's `WebSocket` cannot set an `Authorization` header, so the credential
that opens a socket has to travel in the URL. That is the whole problem this
module solves: a URL is written into nginx's access log, into a proxy's, into the
browser's history and into any error report that quotes the failing request - so
whatever is in it must be worthless by the time anyone reads it. A ticket is:

* **opaque** - 32 random bytes, not a signed token, so there is no key material to
  brute-force and nothing to decode if one is logged;
* **single-use** - `redeem` removes it before it returns, so a logged ticket buys a
  second connection only until the first one is used;
* **brief** - `api.ws_ticket_ttl_s` seconds, defaulting to fifteen, which is long
  enough for a reconnect on a slow link and short enough that an unused ticket is
  dead by the time a log file is rotated;
* **not a credential** - it names an account to the socket layer and nothing else.
  It cannot call an endpoint, and it does not survive the logout cutoff that
  applies to the access token it was minted from, because redeeming re-reads the
  account row (`deps.verify_account`).

Issuing happens on a threadpool thread (a sync route) and redeeming on the event
loop, so the dictionary is guarded by a lock rather than trusted to be atomic.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from smart_parking.core.schemas import Role


def _now() -> datetime:
    """The clock, in one place, so a test of expiry can move it instead of waiting."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TicketGrant:
    """The account a ticket names, as it was when the ticket was minted.

    `issued_at` is kept so the redemption can apply the account's logout cutoff:
    a ticket minted before a sign-out must not open a socket after it.
    """

    user_id: int
    email: str
    role: Role
    issued_at: datetime


class TicketStore:
    """In-process, single-use, expiring. One per application (per worker)."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._grants: dict[str, TicketGrant] = {}
        self._lock = threading.Lock()
        # Tickets presented that were unknown, already used, or too old to spend -
        # reported by `/health` as `ws_ticket_refusals`. A dashboard that will not
        # connect and an account that keeps getting logged out look the same from
        # the browser, and this is the number that says which side is wrong.
        self.refused = 0

    def issue(self, *, user_id: int, email: str, role: Role) -> tuple[str, datetime]:
        """(ticket, expiry) for this caller. The ticket is shown once and never stored elsewhere."""
        value = secrets.token_urlsafe(32)
        now = _now()
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self._lock:
            self._sweep(now)
            self._grants[value] = TicketGrant(
                user_id=user_id, email=email, role=role, issued_at=now
            )
        return value, expires_at

    def redeem(self, value: str) -> TicketGrant | None:
        """The grant behind a ticket, once. Unknown, used and expired all answer None."""
        with self._lock:
            grant = self._grants.pop(value, None)
            if grant is None:
                self.refused += 1
                return None
            if _now() >= grant.issued_at + timedelta(seconds=self.ttl_seconds):
                self.refused += 1
                return None
            return grant

    def outstanding(self) -> int:
        with self._lock:
            return len(self._grants)

    def _sweep(self, now: datetime) -> None:
        """Drop what has expired. Called on the way in, so the store stays bounded
        by the rate of connections rather than by the length of the run."""
        horizon = now - timedelta(seconds=self.ttl_seconds)
        for value, grant in list(self._grants.items()):
            if grant.issued_at <= horizon:
                del self._grants[value]
