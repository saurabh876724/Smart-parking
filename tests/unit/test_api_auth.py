"""Phase 8 credentials: the password, the token, and who may see a plate.

Two halves, because the rules live at two depths. `api/security.py` is plain
Python and its tests call it directly - a redaction rule that can only be shown
through an HTTP response is a rule nobody re-checks when the serializer changes.
The second half drives the endpoints over HTTP, because what is under test is the
wiring: which dependency a route carries, what a refusal costs the caller, and
whether the audit row actually lands in the same transaction as the change.

The security claims this file exists to keep true:

* **No default signing key.** A deployment without one fails at `create_app`
  rather than minting forgeable tokens (see `test_api_config.py`).
* **A refresh token is not an access token.** Same key, same shape, one `use`
  claim apart, and both directions are refused.
* **A plate is masked by role, in one function**, including on the surface where
  it is easiest to forget: a drawn label on an overlay payload.
* **A logout is server-side.** "Sign out" withdraws the access token *and* the
  refresh token by an epoch on the account row, so a credential copied somewhere
  else cannot re-mint itself a session.
* **Guessing is not free.** After the configured failures the endpoint refuses
  before it reads the password hash, because an Argon2id verification costs the
  server as much as it costs the attacker.
* **The socket's URL credential is worthless by the time it is logged.** A ticket
  is opaque, spent once, and expires in seconds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from sqlalchemy import select

from smart_parking.api import deps, throttle, tickets
from smart_parking.api.deps import ApiError
from smart_parking.api.security import (
    ACCESS,
    REFRESH,
    CredentialError,
    PasswordService,
    TokenIssuer,
    check_password_policy,
    mask_label,
    mask_plate,
    valid_email,
)
from smart_parking.api.throttle import MAX_KEYS, LoginThrottle
from smart_parking.api.tickets import TicketStore
from smart_parking.core.config import ApiConfig, PasswordConfig
from smart_parking.core.schemas import Role
from smart_parking.db.models import User
from smart_parking.db.repositories import Repositories
from smart_parking.services.persistence import plate_normaliser_for

from conftest import ACCOUNTS, TEST_SECRET, Api, tokens_for

# -- plate visibility --------------------------------------------------------


def test_the_documented_example_masks_exactly_the_documented_way():
    assert mask_plate("MH20AB1234") == "MH20**1234"


@pytest.mark.parametrize(
    "plate",
    ["MH20AB1234", "DL01CK9999", "MH14ZZ1111", "TS07U9999", "BH20AB1234", "AB01C2345"],
)
def test_at_least_two_characters_are_always_hidden(plate):
    masked = mask_plate(plate)
    hidden = sum(character == "*" for character in masked)
    assert hidden >= 2
    assert len(masked) == len(plate)
    assert plate not in masked
    # What comes back is the head and the tail of the same number, so an operator
    # who knows the plate can still recognise it - and nothing more.
    first, last = masked.index("*"), masked.rindex("*")
    assert plate.startswith(masked[:first]) and plate.endswith(masked[last + 1:])
    assert masked[first:last + 1] == "*" * hidden


def test_a_missing_plate_masks_to_nothing_rather_than_to_a_string():
    assert mask_plate(None) is None
    assert mask_plate("") is None
    assert mask_plate("   ") is None


def test_a_short_plate_is_still_mostly_hidden():
    # 5 characters, 2 of them covered: the middle of a short number is exactly
    # what an overlapping pair of windows would have leaked.
    assert mask_plate("AB123") == "AB**3"


@pytest.mark.parametrize(
    "label, expected",
    [
        ("MH20AB1234 4f2a19", "MH20**1234 4f2a19"),
        ("MH20AB1234 4f2a19 review", "MH20**1234 4f2a19 review"),
        # A lot label is a class name and a score: nothing in it is a plate, and
        # masking it would show an operator `c**` where the answer is "car".
        ("car", "car"),
        ("motorcycle 0.87", "motorcycle 0.87"),
        ("no plate 4f2a19", "no plate 4f2a19"),
        ("", ""),
        (None, None),
    ],
)
def test_a_drawn_label_loses_only_its_plate_half(label, expected):
    assert mask_label(label) == expected


# -- passwords ---------------------------------------------------------------


def test_an_email_is_stored_trimmed_and_lower_cased():
    assert valid_email("  Saurabh@Lot.Example ") == "saurabh@lot.example"


@pytest.mark.parametrize("email", ["", "plain", "a@b", "a@@b.com", "a b@c.com", "@c.com"])
def test_an_address_that_cannot_be_delivered_is_refused(email):
    with pytest.raises(CredentialError) as caught:
        valid_email(email)
    assert caught.value.code == "email_invalid"


def policy(**overrides) -> PasswordConfig:
    # The floor, so a test that changes one cost parameter is comparing two
    # hashes made at costs it can read off the call site.
    return PasswordConfig(**{"memory_kib": 8192, "time_cost": 1, "parallelism": 1, **overrides})


@pytest.mark.parametrize(
    "password, code",
    [
        ("short1!", "password_too_short"),
        ("x" * 129, "password_too_long"),
        (" leading space", "password_whitespace"),
        ("trailing space ", "password_whitespace"),
    ],
)
def test_a_password_a_kdf_should_not_be_asked_about_is_refused_before_hashing(password, code):
    with pytest.raises(CredentialError) as caught:
        check_password_policy(password, policy())
    assert caught.value.code == code


def test_an_account_password_may_not_be_the_account_email():
    with pytest.raises(CredentialError) as caught:
        check_password_policy("Desk@lot.example is mine", policy(), email="desk@lot.example")
    assert caught.value.code == "password_is_email"


def test_the_local_part_rule_only_applies_when_an_email_is_known():
    # The API passes the address; `scripts/create_user.py` may not have one in
    # scope for a policy question, and a blank email must not mean "no check".
    assert check_password_policy("Desk@lot.example is mine", policy()) == "Desk@lot.example is mine"


def test_a_hash_verifies_its_own_password_and_refuses_another():
    service = PasswordService(policy())
    stored = service.hash("Barrier-Ticket-2026", email="desk@lot.example")
    assert service.verify(stored, "Barrier-Ticket-2026")
    assert not service.verify(stored, "Barrier-Ticket-2027")


def test_a_hash_that_cannot_be_read_is_reported_as_a_broken_row():
    service = PasswordService(policy())
    with pytest.raises(CredentialError) as caught:
        service.verify("$2b$12$nonsense-not-argon2", "anything at all")
    assert caught.value.code == "hash_unreadable"


def test_a_hash_made_at_weaker_cost_is_flagged_for_rehash():
    strong = PasswordService(policy(time_cost=5))
    service = PasswordService(policy(time_cost=1))
    stored = strong.hash("Gatehouse-Keystone-3", email="root@lot.example")
    assert service.needs_rehash(stored)
    assert not service.needs_rehash(service.hash("Gatehouse-Keystone-3", email="root@lot.example"))


def test_the_stored_form_is_argon2id_with_the_configured_cost():
    stored = PasswordService(policy(memory_kib=8192, time_cost=2, parallelism=1)).hash(
        "Display-Wall-4471", email="board@lot.example"
    )
    assert stored.startswith("$argon2id$")
    assert "m=8192,t=2,p=1" in stored


# -- tokens ------------------------------------------------------------------


def test_a_short_signing_key_is_refused_by_the_issuer_too():
    api = ApiConfig()
    with pytest.raises(ValueError, match="at least 32 bytes"):
        TokenIssuer(api, "short-key")


def test_a_token_carries_the_account_and_expires_when_configured():
    issuer = TokenIssuer(ApiConfig(access_token_ttl_s=900), TEST_SECRET)
    token = issuer.issue(user_id=7, email="desk@lot.example", role=Role.OPERATOR, use=ACCESS, ttl_seconds=900)
    claims = issuer.decode(token, expect=ACCESS)
    assert (claims.user_id, claims.email, claims.role, claims.use) == (
        7,
        "desk@lot.example",
        Role.OPERATOR,
        ACCESS,
    )
    assert claims.token_id
    lifetime = (claims.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 880 < lifetime <= 900


def test_an_expired_token_says_so_with_a_code_a_client_can_branch_on():
    issuer = TokenIssuer(ApiConfig(), TEST_SECRET)
    token = issuer.issue(user_id=1, email="a@b.co", role=Role.VIEWER, use=ACCESS, ttl_seconds=-1)
    with pytest.raises(CredentialError) as caught:
        issuer.decode(token, expect=ACCESS)
    assert caught.value.code == "token_expired"


def test_a_refresh_token_is_not_an_access_token_and_neither_is_the_converse():
    issuer = TokenIssuer(ApiConfig(), TEST_SECRET)
    access, refresh, _ = issuer.issue_pair(user_id=1, email="a@b.co", role=Role.ADMIN)
    with pytest.raises(CredentialError) as caught:
        issuer.decode(access, expect=REFRESH)
    assert caught.value.code == "token_wrong_use"
    with pytest.raises(CredentialError) as caught:
        issuer.decode(refresh, expect=ACCESS)
    assert caught.value.code == "token_wrong_use"


def test_a_token_signed_with_another_key_is_simply_not_valid():
    other = TokenIssuer(ApiConfig(), "a-completely-different-key-0123456789abcdef")
    forged = other.issue(user_id=1, email="a@b.co", role=Role.ADMIN, use=ACCESS, ttl_seconds=600)
    issuer = TokenIssuer(ApiConfig(), TEST_SECRET)
    with pytest.raises(CredentialError) as caught:
        issuer.decode(forged, expect=ACCESS)
    assert caught.value.code == "token_invalid"


def test_a_token_missing_a_claim_the_system_relies_on_is_refused():
    # Signed with the real key, so the signature is not the problem: `use` is
    # what separates a credential that can call an endpoint from one that cannot.
    payload = {"sub": "1", "email": "root@lot.example", "role": "ADMIN", "iat": 1, "exp": 9999999999}
    naked = jwt.encode(payload, TEST_SECRET, algorithm="HS256")
    with pytest.raises(CredentialError) as caught:
        TokenIssuer(ApiConfig(), TEST_SECRET).decode(naked, expect=ACCESS)
    assert caught.value.code == "token_invalid"


def test_an_unknown_token_use_cannot_be_issued():
    issuer = TokenIssuer(ApiConfig(), TEST_SECRET)
    with pytest.raises(ValueError, match="unknown token use"):
        issuer.issue(user_id=1, email="a@b.co", role=Role.VIEWER, use="session", ttl_seconds=600)


# -- login over HTTP ---------------------------------------------------------


def test_a_correct_password_buys_a_pair_of_tokens_and_names_the_role(api):
    response = api.post("/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"})
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "OPERATOR"
    assert body["email"] == "desk@lot.example"
    assert body["token_type"] == "bearer"
    assert api.ctx.tokens.decode(body["access_token"], expect=ACCESS).role is Role.OPERATOR
    assert api.ctx.tokens.decode(body["refresh_token"], expect=REFRESH).user_id
    assert body["access_expires_at"].endswith("+00:00")


@pytest.mark.parametrize(
    "email, password",
    [
        ("desk@lot.example", "Barrier-Ticket-2027"),
        ("nobody@lot.example", "Barrier-Ticket-2026"),
        ("desk@lot.example", "short"),
        ("not even an address", "Barrier-Ticket-2026"),
    ],
)
def test_a_refused_login_says_the_same_sentence_whatever_went_wrong(api, email, password):
    response = api.post("/auth/login", role=None, json={"email": email, "password": password})
    if email == "not even an address":
        # Refused by the request model, before a password is ever looked at.
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_invalid"
        return
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "bad_credentials", "message": "email or password is not an active account"}
    }


@pytest.mark.parametrize(
    "email, reason",
    [
        ("nobody@lot.example", "unknown_email"),
        ("desk@lot.example", "bad_password"),
    ],
)
def test_the_audit_trail_keeps_the_distinction_the_response_refuses_to_make(api, email, reason):
    api.post("/auth/login", role=None, json={"email": email, "password": "Wrong-Password-1"})
    with api.ctx.database.scope() as session:
        rows = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg)).audit.list(
            action="login_failed"
        )
    assert [row.detail["reason"] for row in rows if row.actor_email == email] == [reason]


def test_a_successful_login_is_audited_and_a_deactivated_account_is_not(api, seeded):
    api.post("/auth/login", role=None, json={"email": "board@lot.example", "password": "Display-Wall-4471"})
    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        # The `tokens` fixture signs every account in to build its headers, so a
        # login row for this address already exists. What is being asserted is the
        # row this call adds, which means knowing which rows were there before it.
        before = {row.id for row in repos.audit.list(action="login", limit=200)}

    response = api.post("/auth/login", role=None, json={"email": "driver@lot.example", "password": "Windy-Aisle-9-2026"})
    assert response.status_code == 200

    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        fresh = [row for row in repos.audit.list(action="login", limit=200) if row.id not in before]
        assert [row.actor_email for row in fresh] == ["driver@lot.example"]
        assert fresh[0].detail == {"role": "CUSTOMER"}
        repos.users.deactivate(repos.users.by_email("board@lot.example").id)

    refused = api.post("/auth/login", role=None, json={"email": "board@lot.example", "password": "Display-Wall-4471"})
    assert refused.status_code == 401
    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        failures = [
            row
            for row in repos.audit.list(action="login_failed")
            if row.actor_email == "board@lot.example"
        ]
    assert [row.detail["reason"] for row in failures] == ["inactive"]


def test_a_login_upgrades_a_hash_made_at_weaker_cost_and_keeps_the_password_working(api, api_database):
    weak = PasswordService(policy(time_cost=4))
    with api_database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        before = repos.users.create("tired@lot.example", weak.hash("Display-Wall-4471", email="tired@lot.example"), Role.VIEWER)
        stored_before = before.password_hash

    assert PasswordService(policy(time_cost=1)).needs_rehash(stored_before)
    assert api.post("/auth/login", role=None, json={"email": "tired@lot.example", "password": "Display-Wall-4471"}).status_code == 200

    with api_database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        after = repos.users.by_email("tired@lot.example")
        assert after.password_hash != stored_before
        assert "t=4" in stored_before and "t=4" not in after.password_hash
        # Re-checked with the same service the app uses, from the row it now reads.
        assert not api.ctx.passwords.needs_rehash(after.password_hash)
        assert api.ctx.passwords.verify(after.password_hash, "Display-Wall-4471")


def test_a_malformed_stored_hash_still_answers_the_wrong_password_sentence(api, api_database):
    with api_database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        repos.users.create("broken@lot.example", "$2b$12$not-argon2-at-all", Role.VIEWER)
    response = api.post("/auth/login", role=None, json={"email": "broken@lot.example", "password": "Display-Wall-4471"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "bad_credentials"
    with api_database.scope() as session:
        rows = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg)).audit.list(
            action="login_failed", actor_email="broken@lot.example"
        )
    assert [row.detail["reason"] for row in rows] == ["hash_unreadable"]


# -- the token on the way back ------------------------------------------------


def test_refreshing_rotates_both_tokens_and_an_access_token_cannot_refresh(api, tokens):
    login = api.post("/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"})
    pair = login.json()

    refreshed = api.post("/auth/refresh", role=None, json={"refresh_token": pair["refresh_token"]})
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["role"] == "OPERATOR"
    assert body["access_token"] != pair["access_token"]
    assert body["refresh_token"] != pair["refresh_token"]

    wrong_way = api.post("/auth/refresh", role=None, json={"refresh_token": pair["access_token"]})
    assert wrong_way.status_code == 401
    assert wrong_way.json()["error"]["code"] == "token_wrong_use"

    # The other direction: a refresh credential in an Authorization header.
    at_endpoint = api.get("/auth/me", token=pair["refresh_token"])
    assert at_endpoint.status_code == 401
    assert at_endpoint.json()["error"]["code"] == "token_wrong_use"


def test_a_refresh_for_an_account_deactivated_since_is_refused(api, tokens):
    who = api.get("/auth/me", role="VIEWER").json()["id"]
    login = api.post("/auth/login", role=None, json={"email": "board@lot.example", "password": "Display-Wall-4471"})
    refresh_token = login.json()["refresh_token"]
    api.post(f"/users/{who}/deactivate", role="ADMIN")
    response = api.post("/auth/refresh", role=None, json={"refresh_token": refresh_token})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "user_inactive"


def test_every_endpoint_but_login_and_refresh_needs_a_token(api, tokens):
    assert api.get("/auth/me", role=None).status_code == 401
    assert api.get("/cameras", role=None).json()["error"]["code"] == "token_missing"
    assert api.get("/cameras", token="not-a-jwt-at-all").json()["error"]["code"] == "token_invalid"


def test_a_token_outlives_its_account_nothing_longer(api, tokens, seeded):
    viewer = api.get("/auth/me", role="VIEWER").json()["id"]
    assert api.get("/sessions", role="VIEWER").status_code == 200
    api.post(f"/users/{viewer}/deactivate", role="ADMIN")
    refused = api.get("/sessions", role="VIEWER")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "user_inactive"


def test_a_demoted_account_cannot_keep_using_a_token_that_says_admin(api, tokens):
    me = api.get("/auth/me", role="VIEWER").json()
    # Minted the way the server mints them, but claiming a role the row does not
    # have - which is what a token issued before a demotion looks like.
    stale = api.ctx.tokens.issue(
        user_id=me["id"], email=me["email"], role=Role.ADMIN, use=ACCESS, ttl_seconds=600
    )
    response = api.get("/users", token=stale)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_changed"


@pytest.mark.parametrize("endpoint", ["/auth/me", "/users/me"])
def test_both_who_am_i_paths_answer_the_same_question(api, endpoint):
    body = api.get(endpoint, role="OPERATOR").json()
    assert body["role"] == "OPERATOR"
    assert body["reveal_plates"] is True
    assert "password" not in str(body).lower()
    assert api.get(endpoint, role="CUSTOMER").json()["reveal_plates"] is False


# -- logout: what a sign-out has to reach -------------------------------------


def test_a_logout_withdraws_the_access_token_and_the_refresh_token_together(api):
    """A logout that only clears the browser's copy is a logout that comes back.

    The tokens are signed, not stored, so the only server-side meaning of "sign
    out" is the epoch on the account row (`users.tokens_invalid_before`) that both
    credentials are now checked against - the access token on every call, the
    refresh token on the one call that would otherwise re-mint it.
    """
    pair = api.post(
        "/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"}
    ).json()
    assert api.get("/auth/me", token=pair["access_token"]).status_code == 200

    out = api.post("/auth/logout", token=pair["access_token"])
    assert out.status_code == 200
    body = out.json()
    assert set(body) == {"revoked_at", "email"}
    assert body["email"] == "desk@lot.example"
    assert body["revoked_at"].endswith("+00:00")

    refused = api.get("/auth/me", token=pair["access_token"])
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "token_revoked"

    # The other half of the pair is the reason this test exists: without the
    # cutoff, a signed-out dashboard keeps a twelve-hour credential that can buy
    # a new session whenever it likes.
    rebuilt = api.post("/auth/refresh", role=None, json={"refresh_token": pair["refresh_token"]})
    assert rebuilt.status_code == 401
    assert rebuilt.json()["error"]["code"] == "token_revoked"


def test_a_logout_reports_the_epoch_the_row_holds_and_leaves_the_account_active(api):
    pair = api.post(
        "/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"}
    ).json()
    body = api.post("/auth/logout", token=pair["access_token"]).json()

    with api.ctx.database.scope() as session:
        row = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg)).users.by_email(
            "desk@lot.example"
        )
        # Signed out, not switched off: a logout costs the credentials, and the
        # account gets its password back to work with.
        assert row.is_active is True
        assert body["revoked_at"] == row.tokens_invalid_before.replace(tzinfo=timezone.utc).isoformat()
    assert api.post("/auth/logout", role=None).json()["error"]["code"] == "token_missing"


def test_a_sign_out_ends_one_account_and_not_the_shift(api):
    """The cutoff is per row, so logging out of the barrier does not log out the wall."""
    with api.ctx.database.scope() as session:
        normalise = plate_normaliser_for(api.ctx.cfg)
        repos = Repositories(session, normalise=normalise)
        desk_id = repos.users.by_email("desk@lot.example").id
        board_id = repos.users.by_email("board@lot.example").id
        assert repos.users.get(board_id).tokens_invalid_before is None

    api.post("/auth/logout", role="OPERATOR")

    for role in ("ADMIN", "VIEWER", "CUSTOMER"):
        assert api.get("/auth/me", role=role).status_code == 200
    assert api.get("/cameras", role="VIEWER").status_code == 200

    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        assert repos.users.get(desk_id).tokens_invalid_before is not None
        assert repos.users.get(board_id).tokens_invalid_before is None


def test_a_logout_is_audited_as_a_sign_out_with_nothing_secret_in_it(api, seeded):
    api.post("/auth/logout", role="VIEWER")
    with api.ctx.database.scope() as session:
        rows = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg)).audit.list(
            action="logout"
        )
    assert [row.actor_email for row in rows] == ["board@lot.example"]
    assert rows[0].detail == {"role": "VIEWER"}
    assert rows[0].target_type == "user"
    assert rows[0].target_id == str(seeded.user_ids["VIEWER"])
    assert "token" not in str(rows[0].detail).lower()


def test_no_account_is_signed_out_before_anyone_asks(api, seeded):
    with api.ctx.database.scope() as session:
        rows = list(session.scalars(select(User)))
    assert len(rows) == len(ACCOUNTS)
    assert all(row.tokens_invalid_before is None for row in rows)


@pytest.mark.parametrize(
    "when, expected",
    [
        # The sign-out's own second is ambiguous, because a JWT's `iat` is a whole
        # number of seconds: 09:00:00.9 was minted after a 09:00:00.4 logout and
        # says the same `iat`. The epoch rounds up, so the ambiguity resolves
        # against the token - the worst case is one extra sign-in.
        (datetime(2026, 10, 12, 9, 0, 0, 400_000, tzinfo=timezone.utc), datetime(2026, 10, 12, 9, 0, 1)),
        (datetime(2026, 10, 12, 9, 0, 1, 500_000, tzinfo=timezone.utc), datetime(2026, 10, 12, 9, 0, 2)),
        (datetime(2026, 10, 12, 9, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 10, 12, 9, 0, 1)),
        # A caller on a clock that keeps its offset: stored in the row's own frame,
        # naive UTC, or the next comparison against it raises instead of deciding.
        (
            datetime(2026, 10, 12, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            datetime(2026, 10, 12, 9, 0, 1),
        ),
    ],
)
def test_the_epoch_is_the_next_whole_second_after_the_signout(api, api_database, when, expected):
    with api_database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        who = repos.users.by_email("desk@lot.example").id
        row = repos.users.invalidate_tokens(who, when)
        assert row.tokens_invalid_before == expected
        session.commit()
        session.expire_all()
        # Read back through the database rather than the object the write left in
        # the identity map: the column is naive UTC (`db/base.py`) and an aware
        # value on a loaded row raises the next time it is compared.
        held = repos.users.get(who)
        assert held.tokens_invalid_before == expected
        assert held.tokens_invalid_before.tzinfo is None


def test_the_epoch_only_ever_moves_forward(api, api_database):
    """A later sign-out must not be reopened by an earlier one arriving late.

    Two tabs, one account: the request that loses the race would otherwise write a
    smaller epoch and put the credentials the winner withdrew back into service.
    """
    late = datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc)
    early = late - timedelta(hours=3)
    # What the row holds, in the column's own frame: naive UTC.
    epoch = late.replace(tzinfo=None) + timedelta(seconds=1)
    with api_database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        who = repos.users.by_email("desk@lot.example").id
        repos.users.invalidate_tokens(who, late)
        again = repos.users.invalidate_tokens(who, early)
        assert again.tokens_invalid_before == epoch
        # A sign-out at the same instant changes nothing either.
        assert repos.users.invalidate_tokens(who, late).tokens_invalid_before == epoch


@pytest.mark.parametrize(
    "issued_at, revoked",
    [
        # The whole of the logout's second is dead, and nothing after it is.
        (datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 12, 8, 59, 59, 999_000, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 12, 9, 0, 1, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 12, 9, 0, 1, 999_000, tzinfo=timezone.utc), False),
    ],
)
def test_the_cutoff_is_strict_at_its_own_boundary(issued_at, revoked):
    """`reject_revoked` in isolation, on a cutoff the test wrote by hand.

    The comparison is what every route and every socket handshake runs, so the
    boundary is worth pinning directly rather than hoping an HTTP call lands on the
    right side of a second. The row is unsaved: the function reads one field.
    """
    row = User(tokens_invalid_before=datetime(2026, 10, 12, 9, 0, 1))

    if revoked:
        with pytest.raises(ApiError) as caught:
            deps.reject_revoked(row, issued_at)
        assert caught.value.code == "token_revoked"
        assert caught.value.status_code == 401
    else:
        deps.reject_revoked(row, issued_at)


# -- accounts ----------------------------------------------------------------


def test_listing_accounts_is_an_admin_question(api):
    assert api.get("/users").status_code == 200
    for role in ("OPERATOR", "VIEWER", "CUSTOMER"):
        refused = api.get("/users", role=role)
        assert refused.status_code == 403
        assert "ADMIN" in refused.json()["error"]["message"]


def test_a_new_account_is_created_hashed_and_audited_without_its_secret(api, api_database):
    created = api.post(
        "/users",
        role="ADMIN",
        json={"email": " Second@Lot.Example ", "password": "Bright-Barrier-Code", "role": "OPERATOR"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["email"] == "second@lot.example"
    assert body["is_active"] is True

    with api_database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        row = repos.users.by_email("second@lot.example")
        assert row.password_hash.startswith("$argon2id$")
        audit = [r for r in repos.audit.list(action="user_created") if r.target_id == str(row.id)]
        assert len(audit) == 1
        assert audit[0].actor_email == "root@lot.example"
        assert audit[0].detail == {"email": "second@lot.example", "role": "OPERATOR"}
        # The whole point of the shape: neither the password nor its hash.
        assert "Second" not in str(audit[0].detail)
        assert "password_hash" not in str(audit[0].detail)

    assert api.post(
        "/auth/login", role=None, json={"email": "second@lot.example", "password": "Bright-Barrier-Code"}
    ).status_code == 200


def test_a_duplicate_address_is_a_conflict_and_a_weak_password_is_a_422(api):
    taken = api.post(
        "/users", json={"email": "desk@lot.example", "password": "Another-Good-Password", "role": "VIEWER"}
    )
    assert taken.status_code == 409
    assert taken.json()["error"]["code"] == "email_taken"

    weak = api.post("/users", json={"email": "new@lot.example", "password": "cat", "role": "VIEWER"})
    assert weak.status_code == 422
    assert weak.json()["error"]["code"] == "password_too_short"

    bogus = api.post("/users", json={"email": "nope", "password": "Another-Good-Password"})
    assert bogus.status_code == 422
    assert bogus.json()["error"]["code"] == "request_invalid"
    message = bogus.json()["error"]["message"]
    # A 422 names the field and nothing else: no submitted value comes back.
    assert message.startswith("email:")
    assert "nope" not in message and "Another-Good-Password" not in message


def test_a_non_admin_cannot_open_an_account(api, tokens):
    for role in ("OPERATOR", "VIEWER", "CUSTOMER"):
        refused = api.post(
            "/users", role=role, json={"email": "x@lot.example", "password": "Some-Good-Password"}
        )
        assert refused.status_code == 403


def test_the_last_active_admin_cannot_be_locked_out(api, seeded):
    admin = api.get("/auth/me", role="ADMIN").json()["id"]
    alone = api.post(f"/users/{admin}/deactivate", role="ADMIN")
    assert alone.status_code == 409
    assert alone.json()["error"]["code"] == "last_admin"
    assert api.get("/users").status_code == 200  # still an admin, still able to work

    relief = api.post(
        "/users", json={"email": "relief@lot.example", "password": "Night-Shift-Keystone", "role": "ADMIN"}
    ).json()
    retired = api.post(f"/users/{admin}/deactivate", role="ADMIN")
    assert retired.status_code == 200
    assert retired.json()["is_active"] is False

    # The door is still open: the account that was created to relieve it works.
    login = api.post(
        "/auth/login", role=None, json={"email": "relief@lot.example", "password": "Night-Shift-Keystone"}
    )
    assert login.status_code == 200
    assert api.get("/users", token=login.json()["access_token"]).status_code == 200
    # ...and the retired one's own token is dead, which is the whole reason the
    # guard exists: the last refusal is the only recoverable kind.
    assert api.get("/users", role="ADMIN").status_code == 401


def test_deactivating_an_unknown_account_is_a_404_and_the_row_is_never_deleted(api):
    missing = api.post("/users/9999/deactivate", role="ADMIN")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "user_unknown"


def test_a_stored_password_hash_is_not_a_field_on_anything(api):
    body = api.get("/users").json()
    assert all(set(row) == {"id", "email", "role", "is_active", "created_at"} for row in body)
    assert not any("hash" in str(row) for row in body)


# -- the socket's credential ---------------------------------------------------


def test_a_socket_ticket_is_minted_for_one_connection_and_buys_nothing_else(api):
    """`POST /auth/ws-ticket` is the only thing that puts a credential in a URL now.

    The handshake itself is covered in `test_api_live.py`; what belongs here is the
    shape of the thing being handed out - short-lived, unlabelled, and useless
    outside the one socket it was minted for.
    """
    response = api.post("/auth/ws-ticket", role="OPERATOR")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ticket", "expires_at"}
    # 32 random bytes, base64url: no structure, no signature, nothing to decode if
    # a proxy log does end up quoting it.
    assert len(body["ticket"]) >= 32
    assert body["expires_at"].endswith("+00:00")
    lifetime = (
        datetime.fromisoformat(body["expires_at"]) - datetime.now(timezone.utc)
    ).total_seconds()
    assert 0 < lifetime <= api.ctx.cfg.api.ws_ticket_ttl_s

    # Not a bearer credential, in either direction.
    refused = api.get("/auth/me", token=body["ticket"])
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "token_invalid"


def test_a_ticket_costs_an_access_token_and_a_refresh_token_cannot_buy_one(api):
    assert api.post("/auth/ws-ticket", role=None).status_code == 401
    pair = api.post(
        "/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"}
    ).json()
    refused = api.post("/auth/ws-ticket", token=pair["refresh_token"])
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "token_wrong_use"


def test_a_ticket_store_hands_a_grant_back_once():
    store = TicketStore(ttl_seconds=15)
    ticket, expires_at = store.issue(user_id=3, email="desk@lot.example", role=Role.OPERATOR)
    assert expires_at.tzinfo is not None

    grant = store.redeem(ticket)
    assert (grant.user_id, grant.email, grant.role) == (3, "desk@lot.example", Role.OPERATOR)
    assert grant.issued_at is not None

    # Spent, and never minted: the same answer, so a probe cannot tell a reused
    # ticket from a guessed one.
    assert store.redeem(ticket) is None
    assert store.redeem("never-minted-at-all") is None
    assert store.refused == 2
    assert store.outstanding() == 0


def test_a_ticket_expires_and_the_store_sweeps_what_nobody_redeemed(monkeypatch):
    started = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tickets, "_now", lambda: started)
    store = TicketStore(ttl_seconds=15)
    first, expires_at = store.issue(user_id=1, email="a@lot.example", role=Role.VIEWER)
    second, _ = store.issue(user_id=2, email="b@lot.example", role=Role.VIEWER)
    assert expires_at == started + timedelta(seconds=15)
    assert store.outstanding() == 2

    # One second short of the TTL still connects: the window is the point, and a
    # reconnect on a slow link lands in it.
    monkeypatch.setattr(tickets, "_now", lambda: started + timedelta(seconds=14))
    assert store.redeem(second) is not None

    monkeypatch.setattr(tickets, "_now", lambda: started + timedelta(seconds=15))
    assert store.redeem(first) is None
    assert store.refused == 1
    # Issuing sweeps on the way in, so the store is bounded by the rate of
    # connections rather than by the length of the run.
    third, _ = store.issue(user_id=3, email="c@lot.example", role=Role.VIEWER)
    assert store.outstanding() == 1
    assert store.redeem(third) is not None


# -- guessing is not free ------------------------------------------------------


class Clock:
    """The throttle's monotonic clock, parked wherever a test says.

    A shipped window is five minutes, and a suite cannot spend it. `throttle._now`
    exists so the sliding window can be tested as arithmetic instead of as a wait.
    """

    def __init__(self) -> None:
        self.t = 1000.0

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def clock(monkeypatch):
    face = Clock()
    monkeypatch.setattr(throttle, "_now", lambda: face.t)
    return face


def test_the_window_slides_instead_of_expiring_as_a_block(clock):
    th = LoginThrottle(window_s=60, max_failures=3)
    key = ("desk@lot.example", "10.0.0.5")
    for _ in range(3):
        th.record_failure(key)
        clock.advance(10)  # attempts at 1000, 1010, 1020; the clock is at 1030

    assert th.retry_after(key) == 30
    clock.advance(25)  # 1055
    assert th.retry_after(key) == 5
    clock.advance(6)  # 1061: the oldest attempt has left the window
    assert th.retry_after(key) is None
    assert th.failures(key) == 2
    th.record_failure(key)  # back to three inside the window
    assert th.retry_after(key) is not None


def test_asking_for_the_wait_does_not_extend_it(clock):
    """`retry_after` is read on every request, so it cannot count as one.

    A read that pushed the deadline out would let a caller who polls the endpoint
    stay blocked forever on a set of failures they stopped making.
    """
    th = LoginThrottle(window_s=60, max_failures=2)
    key = ("desk@lot.example", "10.0.0.5")
    th.record_failure(key)
    th.record_failure(key)
    for _ in range(5):
        assert th.retry_after(key) == 60
    assert th.failures(key) == 2


def test_a_successful_sign_in_clears_one_key_and_not_the_others(clock):
    th = LoginThrottle(window_s=60, max_failures=3)
    mine = ("desk@lot.example", "10.0.0.5")
    other_account = ("board@lot.example", "10.0.0.5")
    other_address = ("desk@lot.example", "10.0.0.9")
    for key in (mine, other_account, other_address):
        th.record_failure(key)
        th.record_failure(key)

    th.record_success(mine)

    # Someone who knows one password must not be able to wipe the counters on the
    # guesses they are still making against other accounts.
    assert th.failures(mine) == 0
    assert th.failures(other_account) == 2
    assert th.failures(other_address) == 2


def test_a_spray_of_addresses_cannot_grow_the_counter_without_limit(clock, monkeypatch):
    """Keys are evicted past `throttle.MAX_KEYS`, so the defence cannot become the
    memory problem it is defending against.

    The bound is lowered for the test: 4096 real keys would make the run a
    benchmark of the same sweep, and the sweep is what is under test.
    """
    monkeypatch.setattr(throttle, "MAX_KEYS", 64)
    assert throttle.MAX_KEYS < MAX_KEYS
    th = LoginThrottle(window_s=60, max_failures=5)
    for n in range(200):
        th.record_failure(("spray@lot.example", f"203.0.113.{n}"))
    assert th.keys() == 64


@pytest.fixture()
def three_guesses(make_client) -> Api:
    """An app that stops tolerating wrong passwords after three.

    The shipped floor is ten per five minutes, which a test of the same rule would
    have to spend ten real Argon2id verifications on. The count is what is under
    test here, not the number.
    """
    client = make_client(
        update=lambda api: api.model_copy(update={"login_throttle_max_failures": 3})
    )
    return Api(client, tokens_for(client))


def test_a_throttled_address_is_refused_before_the_hash_is_read(three_guesses):
    """The point of the throttle: a wrong password costs the server a KDF.

    Production Argon2id is 64 MiB and three passes, so a guesser and the API pay
    the same price per attempt - which makes the login endpoint a denial-of-service
    surface with a password on it. Past the limit the answer is a dictionary
    lookup, and the hash is never touched.
    """
    wrong = {"email": "desk@lot.example", "password": "Wrong-Guess-1"}
    for _ in range(3):
        response = three_guesses.post("/auth/login", role=None, json=wrong)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "bad_credentials"

    blocked = three_guesses.post(
        "/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"}
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"
    assert "try again in" in blocked.json()["error"]["message"]
    wait = int(blocked.headers["retry-after"])
    assert 1 <= wait <= three_guesses.ctx.cfg.api.login_throttle_window_s

    # No audit row while throttled: the failures that earned the wait are already
    # on the trail, and a refusal the caller can repeat forever would grow an
    # append-only table faster than the guesses it is stopping.
    with three_guesses.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(three_guesses.ctx.cfg))
        assert len(repos.audit.list(action="login_failed", actor_email="desk@lot.example")) == 3
        assert [
            row.detail["reason"]
            for row in repos.audit.list(action="login_failed", actor_email="desk@lot.example")
        ] == ["bad_password"] * 3


def test_the_wait_belongs_to_the_address_that_earned_it(three_guesses):
    for _ in range(3):
        three_guesses.post("/auth/login", role=None, json={"email": "desk@lot.example", "password": "Wrong-1"})
    assert three_guesses.post(
        "/auth/login", role=None, json={"email": "desk@lot.example", "password": "Wrong-2"}
    ).status_code == 429

    # A colleague on the same host is not serving the sentence, and an account
    # already signed in is not locked out of its own work.
    ok = three_guesses.post(
        "/auth/login", role=None, json={"email": "board@lot.example", "password": "Display-Wall-4471"}
    )
    assert ok.status_code == 200
    assert three_guesses.get("/auth/me", role="ADMIN").status_code == 200
    assert three_guesses.get("/cameras", role="VIEWER").status_code == 200


def test_a_correct_password_clears_the_guesses_it_ends(three_guesses):
    """An operator who mistyped twice and then got in is not one typo from a wait.

    The success resets only its own key, so the count a real user accrues by
    fumbling their password does not carry into the next session.
    """
    wrong = {"email": "desk@lot.example", "password": "Wrong-Guess-1"}
    for _ in range(2):
        assert three_guesses.post("/auth/login", role=None, json=wrong).status_code == 401
    assert three_guesses.post(
        "/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"}
    ).status_code == 200

    # Three more, and the fourth is the wait again - not the second.
    for _ in range(3):
        assert three_guesses.post("/auth/login", role=None, json=wrong).status_code == 401
    assert three_guesses.post("/auth/login", role=None, json=wrong).status_code == 429


def test_the_shipped_throttle_is_a_policy_in_the_file_and_not_only_in_code(api):
    """An operator who sets `login_throttle_max_failures: 0` would switch the
    defence off by making it refuse everything; the config refuses the value."""
    cfg = api.ctx.cfg.api
    assert cfg.login_throttle_max_failures == 10
    assert cfg.login_throttle_window_s == 300
    assert api.ctx.login_throttle.max_failures == cfg.login_throttle_max_failures
    assert api.ctx.login_throttle.window_s == cfg.login_throttle_window_s
    assert api.ctx.tickets.ttl_seconds == cfg.ws_ticket_ttl_s
    with pytest.raises(ValueError, match="positive"):
        ApiConfig(login_throttle_max_failures=0)
