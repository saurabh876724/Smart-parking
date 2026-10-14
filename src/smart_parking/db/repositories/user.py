"""User rows: the accounts behind a Bearer token (Phase 8).

Hashing is **not** done here. A repository that hashed a password would own two
jobs - storage and credential policy - and the API would have no single place to
answer "why was this password refused". `api/security.py` produces the hash and
this module stores whatever it is handed.

The email rule lives here because both sides need the same one: the API
normalises what it looks up with, and this module normalises what it writes, so
two accounts cannot share a mailbox through a difference in case.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart_parking.core.schemas import Role
from smart_parking.db.models import User


def normalise_email(email: str) -> str:
    """Lower-cased and trimmed - the stored and looked-up form of an address.

    Only case and surrounding space are touched. A local part is
    case-sensitive in the standard, but treating `Saurabh@` and `saurabh@` as two
    accounts that each need their own password is the larger operational risk in
    a parking system, and every provider on this project's address list folds
    case anyway.
    """
    return email.strip().lower()


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def by_email(self, email: str) -> User | None:
        return self.session.scalar(
            select(User).where(User.email == normalise_email(email))
        )

    def create(self, email: str, password_hash: str, role: Role) -> User:
        user = User(
            email=normalise_email(email), password_hash=password_hash, role=role
        )
        self.session.add(user)
        # Flush so the caller can put user.id into a JWT and an audit row inside
        # the same transaction, without the repository committing it.
        self.session.flush()
        return user

    def deactivate(self, user_id: int) -> User | None:
        """Withdraw an account. Never a DELETE - see the model's docstring."""
        user = self.session.get(User, user_id)
        if user is None:
            return None
        user.is_active = False
        self.session.flush()
        return user

    def activate(self, user_id: int) -> User | None:
        user = self.session.get(User, user_id)
        if user is None:
            return None
        user.is_active = True
        self.session.flush()
        return user

    def invalidate_tokens(self, user_id: int, when: datetime) -> User | None:
        """Withdraw every credential issued at or before the sign-out `when`.

        The stored epoch is `when` rounded **up** to the next whole second: a
        JWT's `iat` is truncated to seconds, so an epoch of `100.5` would leave a
        token minted at `100.7` - after the logout, with a truncated `iat` of 100 -
        looking older than the withdrawal. Rounding up costs a token issued in the
        same second as the sign-out, which is a re-login, not a security hole.

        `when` arrives in whatever frame the caller's clock keeps - `utcnow()` is
        naive UTC, a test's is aware - and is stored naive. That is not tidiness:
        SQLite writes an aware value as its own fields and reads it back naive
        (`db/base.py`), so an aware epoch sitting on a loaded row would raise
        `TypeError` the next time this method compared against it.

        The epoch is raised, never lowered: a second sign-out must not re-activate
        what the first one killed.
        """
        user = self.session.get(User, user_id)
        if user is None:
            return None
        naive = when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when
        epoch = (naive + timedelta(seconds=1)).replace(microsecond=0)
        if user.tokens_invalid_before is None or epoch > user.tokens_invalid_before:
            user.tokens_invalid_before = epoch
        self.session.flush()
        return user

    def all(self) -> list[User]:
        return list(self.session.scalars(select(User).order_by(User.email)))

    def count_active_admins(self) -> int:
        return int(
            self.session.scalar(
                select(func.count(User.id)).where(
                    User.role == Role.ADMIN, User.is_active.is_(True)
                )
            )
            or 0
        )

    def total(self) -> int:
        return int(self.session.scalar(select(func.count(User.id))) or 0)
