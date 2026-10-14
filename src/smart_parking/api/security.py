"""Credentials, tokens and plate visibility - no framework in this module.

Split deliberately: everything here is plain Python, so "is this password
acceptable", "does this token mean what it claims" and "may this role see the
plate" are testable without an HTTP client, and the FastAPI wiring in `deps.py`
stays a thin translation layer. A security rule that can only be exercised
through a request is a rule nobody checks when it changes.

Three decisions worth stating because they are the ones an examiner asks about:

* **Argon2id, with the parameters from config.** No bcrypt, no SHA-256 of the
  password, no reversible encryption: a password hash must be slow and must use
  memory, and the cost belongs to the hardware it runs on, so it is a sub-config
  (`api.password`) rather than a constant.
* **The signing key is never in this process by accident.** `core.config.jwt_secret_for`
  raises rather than falling back to a default key, because a hard-coded secret
  is not a missing feature - it is an authentication system anyone who has read
  the source can bypass.
* **A refresh token cannot be used to call an endpoint.** Both are JWTs signed by
  the same key, and the only thing separating them is a `use` claim that
  `TokenIssuer.decode` requires. Accepting either at either endpoint is the
  classic way a long-lived credential ends up in an `Authorization` header.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jose import ExpiredSignatureError, JWTError, jwt

from smart_parking.core.config import ApiConfig, PasswordConfig
from smart_parking.core.schemas import Role

ACCESS = "access"
REFRESH = "refresh"

# Every token must carry all of these. `jwt.decode`'s `options={"require": [...]}`
# is not used because python-jose ignores that key - it validates the signature
# and `exp`, and nothing else - so an unargued token would sail through and then
# fail as a `KeyError` at best, or as a `None` role at worst. Missing claims are
# checked here, explicitly, before any of them is read.
REQUIRED_CLAIMS = ("exp", "iat", "sub", "use", "role")

# Who is shown a full plate number. ADMIN because it is their system; OPERATOR
# because `PATCH /sessions/{id}/plate` is the endpoint where a person retypes a
# misread plate, and masking it from them makes the correction unusable. VIEWER
# and CUSTOMER see the masked form, which is what the doc's UI rule is about.
PLATE_REVEAL_ROLES = frozenset({Role.ADMIN, Role.OPERATOR})

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class CredentialError(ValueError):
    """A password or token that must be refused, with a machine-readable reason.

    `code` is what the API puts in the response body: a client has to be able to
    tell "expired, go refresh" from "malformed, log in again" without reading a
    sentence and guessing.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def valid_email(email: str) -> str:
    """Trimmed-lowercase email, or a CredentialError naming the shape problem.

    A deliberately small rule, not a RFC 5322 parser: an address that has a
    local part, an `@`, a domain and a dotted suffix is storable, and anything
    stricter rejects real addresses more often than it catches typos. Deliverability
    is a different question and this system does not send mail.
    """
    candidate = email.strip().lower()
    if not _EMAIL.match(candidate):
        raise CredentialError("email is not an address of the form name@domain.tld", code="email_invalid")
    return candidate


def check_password_policy(password: str, policy: PasswordConfig, *, email: str | None = None) -> str:
    """Reject the passwords a KDF should never be asked to process.

    Length bounds come from config so the cost is one place, and the
    contains-the-email test is here because a parking system's first operator
    account will be created by someone using their own address as the password.
    """
    if len(password) < policy.min_length:
        raise CredentialError(
            f"password must be at least {policy.min_length} characters", code="password_too_short"
        )
    if len(password) > policy.max_length:
        raise CredentialError(
            f"password must be at most {policy.max_length} characters", code="password_too_long"
        )
    if password.strip() != password:
        # Not a strength judgement: a leading or trailing space is invisible, so
        # it turns a correct password into a failed login that nobody can debug.
        raise CredentialError("password must not start or end with whitespace", code="password_whitespace")
    if email and email.split("@")[0] and email.split("@")[0] in password.lower():
        raise CredentialError("password must not contain the account email", code="password_is_email")
    return password


class PasswordService:
    """Argon2id hashing with the configured cost.

    `verify` reports three outcomes apart, because an API that lumps them
    together cannot tell an operator whether to wait, retype, or migrate a row:
    a wrong password, a malformed stored hash, and a hash made at a different
    cost parameter than this process now uses.
    """

    def __init__(self, policy: PasswordConfig) -> None:
        self.policy = policy
        self._hasher = PasswordHasher(
            memory_cost=policy.memory_kib,
            time_cost=policy.time_cost,
            parallelism=policy.parallelism,
            type=Type.ID,
        )

    def hash(self, password: str, *, email: str | None = None) -> str:
        check_password_policy(password, self.policy, email=email)
        return self._hasher.hash(password)

    def verify(self, stored_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(stored_hash, password)
        except VerifyMismatchError:
            return False
        except (VerificationError, InvalidHashError) as exc:
            raise CredentialError(
                f"stored password hash is not verifiable: {exc}", code="hash_unreadable"
            ) from exc

    def needs_rehash(self, stored_hash: str) -> bool:
        """True when the row was hashed at weaker parameters than this config.

        Surfaced rather than silently applied: re-hashing happens on a successful
        login, which is the only moment the plaintext is available.
        """
        return self._hasher.check_needs_rehash(stored_hash)


@dataclass(frozen=True)
class TokenClaims:
    """What a verified token says. `role` is already a `Role`, not a string.

    Verified here means: signature checks out, it has not expired, every claim
    the system depends on is present, and its `use` is the one the caller
    required. Anything else raises, so a caller never has to remember to look.

    `issued_at` is not decoration. A logout can only withdraw a credential by
    comparing it against the account's cutoff, and the cutoff is a moment in time
    - so the moment the token was minted is part of what a caller has to know
    rather than something it can reconstruct from the signature.
    """

    user_id: int
    email: str
    role: Role
    use: str
    issued_at: datetime
    expires_at: datetime
    token_id: str


class TokenIssuer:
    """HS256 JWTs for one deployment: signed with the key, TTLs from config."""

    def __init__(self, api: ApiConfig, secret: str) -> None:
        self.api = api
        self._secret = secret
        if len(secret.encode()) < 32:
            # Repeated from the resolver on purpose. A caller who built this
            # object directly - a test, or a future script - must not be able to
            # get a weak key past the one place tokens are made.
            raise ValueError("JWT secret must be at least 32 bytes")

    def issue(self, *, user_id: int, email: str, role: Role, use: str, ttl_seconds: int) -> str:
        if use not in (ACCESS, REFRESH):
            raise ValueError(f"unknown token use {use!r}")
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user_id),
            "email": email,
            "role": role.value,
            "use": use,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
            # A unique id per token, so a log line can name the credential that
            # was used without naming the account, and so revocation has a handle.
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(payload, self._secret, algorithm=self.api.jwt_algorithm)

    def issue_pair(self, *, user_id: int, email: str, role: Role) -> tuple[str, str, datetime]:
        """(access, refresh, access expiry) for a fresh login."""
        access = self.issue(
            user_id=user_id, email=email, role=role, use=ACCESS,
            ttl_seconds=self.api.access_token_ttl_s,
        )
        refresh = self.issue(
            user_id=user_id, email=email, role=role, use=REFRESH,
            ttl_seconds=self.api.refresh_token_ttl_s,
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.api.access_token_ttl_s)
        return access, refresh, expires_at

    def decode(self, token: str, *, expect: str) -> TokenClaims:
        """Verify and read a token, insisting on the use the caller requires."""
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[self.api.jwt_algorithm],
            )
        except ExpiredSignatureError as exc:
            raise CredentialError("token has expired", code="token_expired") from exc
        except JWTError as exc:
            # One message for every way a token fails to be what it says: the
            # difference between "bad signature" and "no such claim" is useful to
            # the log and dangerous to the caller.
            raise CredentialError("token is not valid", code="token_invalid") from exc
        missing = [name for name in REQUIRED_CLAIMS if claims.get(name) is None]
        if missing:
            raise CredentialError(
                f"token is missing {', '.join(sorted(missing))}", code="token_invalid"
            )
        if claims.get("use") != expect:
            raise CredentialError(
                f"token is a {claims.get('use')} token, not a {expect} token",
                code="token_wrong_use",
            )
        try:
            role = Role(claims["role"])
            user_id = int(claims["sub"])
            issued_at = datetime.fromtimestamp(claims["iat"], timezone.utc)
            expires_at = datetime.fromtimestamp(claims["exp"], timezone.utc)
        except (ValueError, TypeError, KeyError) as exc:
            raise CredentialError("token claims are not readable", code="token_invalid") from exc
        return TokenClaims(
            user_id=user_id,
            email=claims.get("email", ""),
            role=role,
            use=claims["use"],
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=claims.get("jti", ""),
        )


def mask_plate(plate: str | None) -> str | None:
    """`MH20AB1234` -> `MH20**1234`, which is the doc's own example reproduced.

    Masking is done here, in one function, rather than in each response model:
    the rule has to be the same for a session detail, a plate search and a
    WebSocket push, and three implementations are three chances to leak.

    One constraint fixes the shape: **at least two characters are always
    hidden**. Without it a short plate becomes readable through two windows that
    overlap in the middle, which is the failure this function exists to prevent.
    """
    if not plate:
        return None
    text = plate.strip()
    n = len(text)
    if n == 0:
        return None
    hidden = min(n, max(2, n - 8))
    visible = n - hidden
    head = (visible + 1) // 2
    tail = visible - head
    return text[:head] + "*" * hidden + (text[n - tail:] if tail else "")


def _is_plate_token(token: str) -> bool:
    """Does this word look like a registration number rather than a label?

    The check exists because the same helper redacts a **gate** label
    (`"MH20AB1234 4f2a19"`) and leaves a **lot** label alone (`"car"`). Masking
    the second would turn a class name into `"c**"`, which reads as a redacted
    plate to an operator and hides nothing. A normalised Indian plate is one
    long upper-case alphanumerics with at least one digit in it.
    """
    return len(token) >= 6 and token.isalnum() and any(character.isdigit() for character in token)


def mask_label(label: str | None) -> str | None:
    """Redact the plate half of a track label: `"MH20AB1234 4f2a19"` -> masked.

    A drawn label is `<plate> <session tail>`, so only its first token can be
    sensitive. This exists because the polled `/overlay` and the overlay
    WebSocket hand out the same label, and a redaction rule written twice is a
    rule that will be applied to only one of them.
    """
    if not label:
        return label
    head, sep, tail = label.partition(" ")
    if not _is_plate_token(head):
        return label
    return f"{mask_plate(head)}{sep}{tail}"
