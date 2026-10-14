"""How many times an address may guess a password before it has to wait.

This exists for two reasons, in this order:

* **An Argon2id verification is expensive on purpose.** Sixty-four mebibytes and
  three passes per attempt means a refused login costs the server as much as an
  accepted one, which is exactly what makes it a denial-of-service surface. A
  throttle refuses *before* the hash is read, so the cost of the three-hundredth
  wrong password is a dictionary lookup.
* **It bounds how long one account can be guessed at** without the response
  becoming an oracle: the answer on success and on failure stays the same
  sentence, and the audit trail keeps the distinction.

What it is **not**: a distributed defence. The counters live in this process, so
a second uvicorn worker has its own, and a restart empties them. Sliding the
window per `(email, client address)` is the honest shape for one API serving one
lot; an internet-facing deployment would want the same rule with a shared store
(Redis or nginx's own `limit_req`, which is already configured in
`deploy/nginx.conf` for request rate). `api.login_throttle_*` sets the window and
the count.

Keys are bounded so that a spray of distinct addresses cannot grow the dictionary
without limit: the sweep prunes a key once its whole window has passed, then evicts
oldest-first past `MAX_KEYS`.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Deque, Hashable

# Enough independent accounts to be awkward for a spray and small enough to sit
# in memory without a plan for it. Eviction only happens past this many keys.
MAX_KEYS = 4096


def _now() -> float:
    """The clock, in one place, so a test of the sliding window can move it.

    Monotonic rather than wall-clock on purpose: a window that jumped backwards
    when NTP corrected this machine would unlock the throttle for an attacker who
    could not otherwise wait it out.
    """
    return time.monotonic()


class LoginThrottle:
    """A sliding window of failures per key, with the wait it owes."""

    def __init__(self, *, window_s: int, max_failures: int) -> None:
        self.window_s = window_s
        self.max_failures = max_failures
        self._failures: dict[Hashable, Deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: Hashable) -> int | None:
        """Seconds this key must wait, or None while it may still try.

        Read-only: a key that has spent its window is not counted again here, so
        calling this every request cannot push the deadline out forever.
        """
        with self._lock:
            now = _now()
            attempts = self._prune(key, now)
            if attempts is None or len(attempts) < self.max_failures:
                return None
            return max(1, math.ceil(attempts[0] + self.window_s - now))

    def record_failure(self, key: Hashable) -> None:
        now = _now()
        with self._lock:
            self._failures.setdefault(key, deque()).append(now)
            # After the insert, so `MAX_KEYS` is the bound and not a target the
            # dictionary overshoots on every attempt past it.
            self._sweep_expired(now)

    def record_success(self, key: Hashable) -> None:
        """A correct password clears the account's past failures.

        Only the key that just succeeded is cleared, so an attacker who happens to
        know one password cannot reset the counter on the guesses they are still
        making against other addresses.
        """
        with self._lock:
            self._failures.pop(key, None)

    def failures(self, key: Hashable) -> int:
        """How many attempts are in the current window - for a log line, not a response."""
        with self._lock:
            attempts = self._prune(key, _now())
            return len(attempts) if attempts else 0

    def keys(self) -> int:
        with self._lock:
            return len(self._failures)

    # -- internals (called with the lock held) --------------------------------

    def _prune(self, key: Hashable, now: float) -> Deque[float] | None:
        attempts = self._failures.get(key)
        if attempts is None:
            return None
        cutoff = now - self.window_s
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
            return None
        return attempts

    def _sweep_expired(self, now: float) -> None:
        for key in list(self._failures):
            self._prune(key, now)
        # A spray of unknown addresses would otherwise add a key per attempt.
        while len(self._failures) > MAX_KEYS:
            self._failures.pop(next(iter(self._failures)))
