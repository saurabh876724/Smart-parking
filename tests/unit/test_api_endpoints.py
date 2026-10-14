"""Phase 8 HTTP surface: what each endpoint answers, refuses, and discloses.

Organised by the doc's own endpoint groups. Three kinds of assertion recur, and
each is a rule the doc states rather than a preference of this file:

* **the refusal carries a code**, not just a status - a client branches on
  `error.code`, so the string is as much API as the status number is;
* **a plate is masked by default**, and the roles that may see the full text are
  the two that have a reason to;
* **a write is audited with what changed**, and the audit detail carries the
  masked form - the trail is read back by an endpoint, so a plate recorded there
  would be a second plate database with no rules on it.

Nothing here needs YOLO, a camera or a GPU: `api.pipeline` is emptied by the
`api_config` fixture and the streams are asserted against their own refusals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text

from conftest import Api
from smart_parking.api import deps
from smart_parking.api.schemas import SessionResponse
from smart_parking.api.security import mask_plate
from smart_parking.core.schemas import SessionStatus
from smart_parking.db.models import ParkingSession
from smart_parking.db.repositories import Repositories
from smart_parking.services.persistence import plate_normaliser_for

FULL = "MH20AB1234"
MASKED = "MH20**1234"


def audit_rows(api: Api, action: str):
    with api.ctx.database.scope() as session:
        return Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg)).audit.list(
            action=action, limit=50
        )


def error(response):
    return response.json()["error"]


def rest_routes(app) -> list[tuple[str, "APIRoute"]]:
    """`(full path, route)` for every REST route, through FastAPI's lazy mount.

    0.142 keeps an included router as one `_IncludedRouter` entry in `app.routes`
    instead of copying its routes in, so a flat `isinstance(route, APIRoute)` over
    `app.routes` matches nothing - and a test built on it silently concludes that
    every route is anonymous. The prefix passed to `include_router` lives on the
    wrapper's include context, and the router's own prefix is already in the inner
    paths, so both have to be collected.
    """
    out: list[tuple[str, APIRoute]] = []
    stack: list[tuple[list, str]] = [(list(app.routes), "")]
    while stack:
        routes, prefix = stack.pop()
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                extra = getattr(getattr(route, "include_context", None), "prefix", "") or ""
                stack.append((list(inner.routes), prefix + extra))
                continue
            if isinstance(route, APIRoute):
                out.append((prefix + route.path, route))
    return out


# -- cameras ------------------------------------------------------------------


def test_the_camera_list_answers_the_question_asking_for_cameras_actually_asks(api, seeded):
    """Configuration and live state in one payload, per camera."""
    body = api.get("/cameras").json()
    assert {row["camera_id"] for row in body} == {"lot_cam1", "gate_cam1"}
    gate = next(row for row in body if row["camera_id"] == "gate_cam1")
    assert gate["role"] == "GATE_ENTRY"
    assert gate["gate_file"] == "configs/gates/gate_a.json"
    assert gate["live"]["streaming"] is False
    assert gate["live"]["frames_available"] is False
    assert gate["live"]["role"] == "GATE_ENTRY"
    assert gate["live"]["error"] is None


def test_an_unregistered_camera_is_a_404_that_names_the_id(api):
    refused = api.get("/cameras/nope")
    assert refused.status_code == 404
    assert error(refused) == {"code": "camera_unknown", "message": "no camera 'nope' is registered"}


def test_registering_a_camera_is_admin_work_that_does_not_start_a_stream(api, seeded):
    created = api.post(
        "/cameras",
        json={"camera_id": "lot_cam9", "name": "Patio", "role": "LOT", "source": "data/samples/lot_shot1.mp4"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["camera_id"] == "lot_cam9"
    assert body["enabled"] is True
    assert body["fps_limit"] == 15.0
    assert body["slots_file"] is None
    # A row is configuration. The cameras that run are `api.pipeline.cameras`,
    # which is a decision about this machine's GPU, not about a database row.
    assert api.get("/cameras/lot_cam9").json()["live"]["streaming"] is False
    rows = audit_rows(api, "camera_registered")
    assert [row.target_id for row in rows] == ["lot_cam9"]
    assert rows[0].detail["role"] == "LOT"


@pytest.mark.parametrize("role", ["OPERATOR", "VIEWER", "CUSTOMER"])
def test_a_non_admin_cannot_register_a_camera(api, seeded, role):
    assert api.post("/cameras", role=role, json={"camera_id": "x1", "role": "LOT"}).status_code == 403


def test_a_camera_role_the_enum_does_not_have_is_refused_before_it_is_stored(api):
    bogus = api.post("/cameras", json={"camera_id": "lot_cam8", "role": "GATEWAY"})
    assert bogus.status_code == 422
    assert error(bogus)["code"] == "request_invalid"
    assert "GATEWAY" not in error(bogus)["message"]


def test_a_duplicate_camera_id_is_a_conflict(api, seeded):
    taken = api.post("/cameras", json={"camera_id": "lot_cam1", "role": "LOT"})
    assert taken.status_code == 409
    assert error(taken)["code"] == "camera_exists"


def test_a_patch_changes_only_the_fields_it_named_and_records_only_those_names(api, seeded):
    before = api.get("/cameras/gate_cam1").json()
    patched = api.patch("/cameras/gate_cam1", json={"name": "Barrier lane", "fps_limit": 8.0})
    assert patched.status_code == 200
    after = patched.json()
    assert after["name"] == "Barrier lane"
    assert after["fps_limit"] == 8.0
    for field in ("role", "source", "gate_file", "camera_id"):
        assert after[field] == before[field]
    assert audit_rows(api, "camera_updated")[0].detail == {"changed": ["fps_limit", "name"]}


def test_a_patch_that_names_nothing_is_refused_rather_than_a_quiet_no_op(api, seeded):
    empty = api.patch("/cameras/lot_cam1", json={})
    assert empty.status_code == 422
    assert error(empty)["code"] == "nothing_to_change"


def test_disabling_a_camera_is_a_patch_and_an_audit_row(api, seeded):
    off = api.patch("/cameras/lot_cam1", json={"enabled": False}, role="OPERATOR")
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert audit_rows(api, "camera_updated")[0].detail == {"changed": ["enabled"]}


def test_restarting_is_refused_by_a_server_that_runs_no_cameras(api, seeded):
    refused = api.post("/cameras/lot_cam1/restart")
    assert refused.status_code == 409
    assert error(refused) == {"code": "pipelines_disabled", "message": "this server is not running any cameras"}


@pytest.mark.parametrize(
    "path",
    ["/cameras/lot_cam1/snapshot", "/cameras/lot_cam1/stream.mjpg", "/cameras/lot_cam1/overlay"],
)
def test_the_three_ways_to_watch_a_camera_all_say_there_is_nothing_to_watch(api, seeded, path):
    refused = api.get(path, role="VIEWER")
    assert refused.status_code == 409
    assert error(refused)["code"] == "no_frame"


# -- slots --------------------------------------------------------------------


def test_the_bay_list_can_be_narrowed_by_camera_state_and_type(api, seeded):
    everything = api.get("/slots").json()
    assert {row["slot_code"] for row in everything} == {"A1", "A2", "A3", "A4"}
    assert api.get("/slots", params={"camera_id": "gate_cam1"}).json() == []
    occupied = api.get("/slots", params={"status": "OCCUPIED"}).json()
    assert [row["slot_code"] for row in occupied] == ["A2"]
    reserved = api.get("/slots", params={"type": "RESERVED"}).json()
    assert [row["slot_code"] for row in reserved] == ["A4"]


def test_a_bay_that_a_visit_holds_says_which_visit(api, seeded):
    rows = {row["slot_code"]: row for row in api.get("/slots").json()}
    assert rows["A2"]["session_id"] == "gate-0001"
    assert rows["A1"]["session_id"] is None
    # The FSM's committed state, not a debounce: PENDING_* is never a value here.
    assert rows["A2"]["occupied_by_track_id"] == 11
    assert rows["A3"]["current_status"] == "UNKNOWN"


def test_drawing_a_bay_by_hand_is_admin_work_and_is_audited(api, seeded):
    created = api.post(
        "/slots",
        json={
            "camera_id": "lot_cam1",
            "slot_code": "A9",
            "slot_type": "EV",
            "polygon": [[0, 0], [50, 0], [50, 40], [0, 40]],
            "frame_size": {"width": 480, "height": 360},
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["slot_code"] == "A9" and body["slot_type"] == "EV"
    assert body["current_status"] == "UNKNOWN"
    assert body["polygon"][0] == [0.0, 0.0]
    rows = audit_rows(api, "slot_created")
    assert rows[0].detail == {"camera_id": "lot_cam1", "slot_code": "A9", "points": 4}
    assert rows[0].target_id == str(body["id"])


def test_a_bay_can_only_be_drawn_on_a_camera_that_exists(api):
    refused = api.post(
        "/slots", json={"camera_id": "nowhere", "slot_code": "Z1", "polygon": [[0, 0], [9, 0], [9, 9]]}
    )
    assert refused.status_code == 404
    assert error(refused)["code"] == "camera_unknown"


def test_the_same_bay_code_on_the_same_camera_is_a_conflict(api, seeded):
    taken = api.post(
        "/slots", json={"camera_id": "lot_cam1", "slot_code": "A2", "polygon": [[0, 0], [9, 0], [9, 9]]}
    )
    assert taken.status_code == 409
    assert error(taken)["code"] == "slot_exists"


@pytest.mark.parametrize(
    "polygon, message",
    [
        ([[0, 0], [9, 0]], "at least 3 items"),
        ([[0, 0], [9, 0], [9, 9, 1]], "a polygon point is"),
    ],
)
def test_a_shape_that_is_not_a_polygon_is_refused(api, seeded, polygon, message):
    refused = api.post(
        "/slots", json={"camera_id": "lot_cam1", "slot_code": "B1", "polygon": polygon}
    )
    assert refused.status_code == 422
    assert message in error(refused)["message"]


def test_an_operator_can_redraw_a_bay_without_changing_what_is_in_it(api, seeded):
    before = {row["slot_code"]: row for row in api.get("/slots").json()}["A1"]
    moved = api.put(
        f"/slots/{before['id']}/polygon",
        role="OPERATOR",
        json={"polygon": [[1, 1], [99, 1], [99, 50], [1, 50]], "frame_size": {"width": 640, "height": 480}},
    )
    assert moved.status_code == 200
    after = moved.json()
    assert after["polygon"][1] == [99.0, 1.0]
    # The next FSM tick re-decides this bay against the new shape; rewriting the
    # status here would be the endpoint inventing an observation.
    assert after["current_status"] == before["current_status"] == "FREE"
    row = audit_rows(api, "slot_polygon_updated")[0]
    assert row.detail == {"camera_id": "lot_cam1", "slot_code": "A1", "points": 4}


def test_redrawing_a_bay_that_was_never_drawn_is_a_404(api):
    refused = api.put("/slots/99999/polygon", json={"polygon": [[0, 0], [1, 0], [1, 1]]})
    assert refused.status_code == 404
    assert error(refused)["code"] == "slot_unknown"


def test_a_lot_summary_does_not_count_a_camera_that_is_not_reporting_as_spare_capacity(api, seeded):
    body = api.get("/lots/lot_cam1/summary").json()
    assert body == {
        "camera_id": "lot_cam1",
        "total": 4,
        "occupied": 1,
        "available": 2,
        "reserved": 1,
        "unknown": 1,
        # 1 of the 3 bays that have a committed state, not 1 of 4: A3 says nothing.
        "occupancy_rate": pytest.approx(1 / 3),
        "from_live_frames": False,
    }
    assert api.get("/lots/gate_cam1/summary").json()["total"] == 0
    missing = api.get("/lots/nope/summary")
    assert missing.status_code == 404
    assert error(missing)["code"] == "camera_unknown"


# -- sessions -----------------------------------------------------------------


def test_the_session_page_is_ordered_newest_first_and_carries_its_own_size(api, seeded):
    body = api.get("/sessions").json()
    assert body["total"] == 4
    assert body["limit"] == api.ctx.cfg.api.page_size
    assert body["offset"] == 0
    assert [item["source_session_id"] for item in body["items"]] == [
        "gate-0004",
        "gate-0003",
        "gate-0002",
        "gate-0001",
    ]


#: The ten cells the Recent Sessions table draws, by the names the JSON carries.
#:
#: The dashboard used to read `session_id`, `plate_number` and `duration_s`, none
#: of which the endpoint has ever answered with, and every column was blank - a
#: mistake JSON could not complain about, because a missing key reads as `None`
#: in Python and `undefined` in TypeScript. This list is the contract; the client
#: keeps the same one in `frontend/src/lib/api.ts::SessionRecord`.
SESSION_ROW_KEYS = [
    "anomaly_reason",
    "camera_id",
    "duration_seconds",
    "entry_time",
    "entry_track_id",
    "exit_reason",
    "exit_time",
    "exit_track_id",
    "id",
    "ocr_confidence",
    "plate",
    "plate_confidence",
    "plate_masked",
    "review_reason",
    "source_session_id",
    "status",
    "vehicle_type",
]


def test_a_session_row_names_its_fields_the_way_the_dashboard_reads_them(api, seeded):
    body = api.get("/sessions").json()
    assert sorted(body["items"][0]) == SESSION_ROW_KEYS
    # The DTO and the wire agree, so an added column cannot be left unserialised
    # (or a renamed one survive in the model while the JSON quietly loses it).
    assert sorted(SessionResponse.model_fields) == SESSION_ROW_KEYS


def test_a_session_row_leaves_no_cell_that_the_table_reads_undefined(api, seeded):
    # A field that has a default still has to be *in the body*, or the client's
    # `row.exit_reason` is `undefined` and the column goes blank for the one visit
    # the endpoint had something to say about.
    body = api.get("/sessions").json()
    live = next(item for item in body["items"] if item["source_session_id"] == "gate-0001")
    assert sorted(live) == SESSION_ROW_KEYS
    assert live["status"] == SessionStatus.ACTIVE.value
    assert live["plate_masked"] == MASKED
    assert live["exit_time"] is None
    assert live["duration_seconds"] is None
    # The orphan exit is the row whose measured arrival does not exist, so the
    # same fifteen keys have to be there with nothing in the middle of them.
    orphan = next(item for item in body["items"] if item["source_session_id"] == "gate-0004")
    assert sorted(orphan) == SESSION_ROW_KEYS
    assert orphan["entry_time"] is None
    assert orphan["anomaly_reason"] == "exit_without_entry"


def test_a_client_cannot_ask_for_more_rows_than_the_deployment_allows(api, seeded):
    clamped = api.get("/sessions", params={"limit": 10000}).json()
    assert clamped["limit"] == api.ctx.cfg.api.page_size
    # Paging is still possible, and `total` is the whole filter's count, not the
    # page's: a client that read `total` as "rows left" would stop early.
    first = api.get("/sessions", params={"limit": 1, "offset": 0}).json()
    second = api.get("/sessions", params={"limit": 1, "offset": 1}).json()
    assert first["total"] == second["total"] == 4
    assert first["items"][0]["id"] != second["items"][0]["id"]


def aware(value: datetime) -> str:
    """A stored naive-UTC timestamp as the ISO text a client would send back."""
    return value.replace(tzinfo=timezone.utc).isoformat()


def test_the_window_filters_are_named_from_and_to_because_that_is_what_a_chart_uses(api, seeded):
    # The window is over *entry* time, so the visit that arrived two hours ago is
    # inside a 150-minute one and the two older closed visits are not.
    recent = api.get("/sessions", params={"from": aware(seeded.at(150))}).json()
    assert [item["source_session_id"] for item in recent["items"]] == ["gate-0001"]
    # The orphan exit has no measured arrival, so no window can contain it: a
    # filter that returned it would be claiming an arrival nobody saw.
    wide = api.get("/sessions", params={"from": aware(seeded.at(600))}).json()
    assert {item["source_session_id"] for item in wide["items"]} == {
        "gate-0001",
        "gate-0002",
        "gate-0003",
    }
    narrow = api.get(
        "/sessions", params={"from": aware(seeded.at(400)), "to": aware(seeded.at(300))}
    ).json()
    assert [item["source_session_id"] for item in narrow["items"]] == ["gate-0002"]


def test_status_and_camera_narrow_the_history(api, seeded):
    active = api.get("/sessions", params={"status": "ACTIVE"}).json()
    assert [item["source_session_id"] for item in active["items"]] == ["gate-0001"]
    assert api.get("/sessions", params={"camera_id": "lot_cam1"}).json()["total"] == 0
    assert api.get("/sessions", params={"camera_id": "gate_cam1"}).json()["total"] == 4


def test_a_plate_that_is_not_in_the_system_is_an_empty_page_not_an_answer(api, seeded):
    """A 404 here would tell a caller whether a number is parked on site."""
    assert api.get("/sessions", params={"plate": "MH99ZZ9999"}).json() == {
        "total": 0,
        "limit": api.ctx.cfg.api.page_size,
        "offset": 0,
        "items": [],
    }
    found = api.get("/sessions", params={"plate": FULL}).json()
    assert [item["source_session_id"] for item in found["items"]] == ["gate-0001"]


@pytest.mark.parametrize("role, reveal", [("ADMIN", True), ("OPERATOR", True), ("VIEWER", False), ("CUSTOMER", False)])
def test_only_the_two_roles_that_have_a_reason_to_read_a_plate_do(api, seeded, role, reveal):
    body = api.get(f"/sessions/{seeded.session_ids['active']}", role=role).json()
    assert (body["plate"] == FULL) is reveal
    assert body["plate_masked"] == MASKED
    assert body["vehicle_type"] == "car"
    assert body["status"] == "ACTIVE"
    assert body["exit_time"] is None


def test_an_orphan_exit_keeps_the_shape_of_the_anomaly_it_is(api, seeded):
    body = api.get(f"/sessions/{seeded.session_ids['orphan']}", role="OPERATOR").json()
    assert body["status"] == "ANOMALY"
    assert body["anomaly_reason"] == "exit_without_entry"
    assert body["entry_time"] is None
    assert body["duration_seconds"] is None
    assert body["plate"] is None and body["plate_masked"] is None


def test_an_unknown_visit_is_a_404(api):
    refused = api.get("/sessions/987654")
    assert refused.status_code == 404
    assert error(refused)["code"] == "session_unknown"


def test_retyping_a_plate_replaces_it_and_the_trail_keeps_only_the_masked_forms(api, seeded):
    session_id = seeded.session_ids["short"]
    corrected = api.patch(f"/sessions/{session_id}/plate", role="OPERATOR", json={"plate": "dl01-ck8888"})
    assert corrected.status_code == 200
    # The same normaliser the OCR stage uses, so a space or dash retyped by hand
    # is not a second plate number.
    assert corrected.json()["plate"] == "DL01CK8888"
    row = audit_rows(api, "plate_corrected")[0]
    assert row.detail["before"] == mask_plate("DL01CK9999")
    assert row.detail["after"] == mask_plate("DL01CK8888")
    assert row.detail["session"] == "gate-0002"
    assert "DL01CK8888" not in str(row.detail)


def test_an_operator_cannot_teach_the_endpoint_a_plate_the_validator_refuses(api, seeded):
    refused = api.patch(
        f"/sessions/{seeded.session_ids['short']}/plate", role="OPERATOR", json={"plate": "12345678"}
    )
    assert refused.status_code == 422
    assert error(refused)["code"] == "plate_no_pattern_match"
    assert api.get(f"/sessions/{seeded.session_ids['short']}").json()["plate"] == "DL01CK9999"


def test_a_plate_correction_needs_the_role_that_will_be_audited_for_it(api, seeded):
    for role in ("VIEWER", "CUSTOMER"):
        assert api.patch(
            f"/sessions/{seeded.session_ids['short']}/plate", role=role, json={"plate": FULL}
        ).status_code == 403


def test_force_closing_records_the_human_and_refuses_to_claim_the_gate_measured_it(api, seeded):
    closed = api.post(f"/sessions/{seeded.session_ids['active']}/close", role="OPERATOR", json={})
    assert closed.status_code == 200
    body = closed.json()
    assert body["status"] == "COMPLETED"
    # "line" and "dwell" are the gate's vocabulary. This is not the gate.
    assert body["exit_reason"] is None
    assert body["review_reason"] == "force closed by desk@lot.example"
    assert body["duration_seconds"] > 0
    assert body["exit_time"] is not None
    row = audit_rows(api, "session_force_closed")[0]
    assert row.detail["session"] == "gate-0001"
    assert row.detail["reason"] == "manual"


def test_an_exit_time_is_accepted_only_when_it_is_after_the_entry(api, seeded):
    early = api.post(
        f"/sessions/{seeded.session_ids['long']}/close",
        role="OPERATOR",
        json={"exit_time": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
    )
    assert early.status_code == 409
    assert error(early)["code"] == "already_closed"

    # A fresh visit, closed with a time before it arrived.
    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        arrival = repos.sessions.open("gate-0100", "gate_cam1", 60, seeded.at(600))
    refused = api.post(
        f"/sessions/{arrival.id}/close",
        role="OPERATOR",
        json={"exit_time": seeded.at(700).replace(tzinfo=timezone.utc).isoformat(), "reason": "wrong gate"},
    )
    assert refused.status_code == 422
    assert error(refused)["code"] == "exit_before_entry"


def test_a_visit_with_no_measured_entry_is_never_closed_by_an_api_call(api, seeded):
    """The duration would be invented from nothing, so this is a 409 and a reason.

    The row is written directly because every repository path that leaves
    `entry_time` NULL gives it an `exit_time` as well, and a visit with *neither*
    is what the guard is really about.
    """
    with api.ctx.database.scope() as session:
        neither = ParkingSession(
            source_session_id="gate-0200",
            camera_id="gate_cam1",
            entry_track_id=60,
            status=SessionStatus.ANOMALY,
            anomaly_reason="entry_never_measured",
        )
        session.add(neither)
        session.flush()
        neither_id = neither.id

    refused = api.post(f"/sessions/{neither_id}/close", role="ADMIN", json={})
    assert refused.status_code == 409
    assert error(refused)["code"] == "no_entry_time"
    # The seeded orphan is refused for the other honest reason: it does have an
    # exit, so there is nothing left for an operator to close.
    already = api.post(f"/sessions/{seeded.session_ids['orphan']}/close", role="ADMIN", json={})
    assert error(already)["code"] == "already_closed"


def test_the_two_ways_to_see_ones_own_account_answer_the_same_question(api, seeded):
    for path in ("/auth/me", "/users/me"):
        body = api.get(path, role="CUSTOMER").json()
        assert body["email"] == "driver@lot.example"
        assert body["reveal_plates"] is False
        assert "password" not in str(body).lower()


# -- plates -------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ADMIN", "OPERATOR", "VIEWER", "CUSTOMER"])
def test_a_plate_search_is_masked_for_everyone_including_the_admin(api, seeded, role):
    """A search walks a table a hundred rows at a time; a plate is disclosed where
    an audit row names the person."""
    body = api.get("/plates/search", params={"q": "MH1"}, role=role).json()
    assert body["total"] == 1
    assert [row["plate"] for row in body["items"]] == [None]
    assert [row["plate_masked"] for row in body["items"]] == [mask_plate("MH14ZZ1111")]


def test_the_search_answers_with_the_sightings_the_system_counted(api, seeded):
    body = api.get("/plates/search", params={"q": "20AB1234"}).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["reads"] == 3
    # `confidence` is the best score ever seen, not the latest - a plate read once
    # at 0.93 is not a 0.6 plate because a later frame was blurrier.
    assert row["confidence"] == pytest.approx(0.93)
    assert row["first_seen_at"] < row["last_seen_at"]


def test_a_fragment_of_two_is_a_dump_attempt_and_is_refused(api, seeded):
    for query in ("M", "", "   ", "MH"):
        refused = api.get("/plates/search", params={"q": query})
        assert refused.status_code == 422
        assert error(refused)["code"] == "query_too_short"


def test_a_query_of_only_separators_is_refused_rather_than_matching_everything(api, seeded):
    """`%%%` is three characters and normalises to nothing, which as a LIKE
    pattern would be every row in the table."""
    for query in ("%%%", "___", "- - -", "MH-%"):
        refused = api.get("/plates/search", params={"q": query})
        assert refused.status_code == 422, query
        assert error(refused)["code"] == "query_too_short"


def test_a_search_finds_a_plate_written_the_way_a_person_types_it(api, seeded):
    for query in ("mh 20 ab 1234", "MH-20-AB-1234", "mh20ab1234"):
        body = api.get("/plates/search", params={"q": query}).json()
        assert [row["plate_masked"] for row in body["items"]] == [MASKED], query


# -- statistics ---------------------------------------------------------------


def test_the_occupancy_series_counts_intervals_that_covered_each_sample(api, seeded):
    body = api.get("/stats/occupancy", params={"bucket": "hour"}).json()
    assert body["bucket"] == "hour"
    assert body["definition"] == "bays whose occupancy interval covered the sample instant"
    # A1's closed cycle and A2's still-open interval are both inside the default
    # 24-hour window; the open one is a row the chart reports as it is.
    assert body["intervals_considered"] == 2
    assert body["points"][0].keys() == {"at", "occupied"}
    assert api.get("/stats/occupancy", params={"camera_id": "gate_cam1"}).json()["intervals_considered"] == 0


def test_a_bucket_is_a_choice_from_a_list_not_a_number(api, seeded):
    refused = api.get("/stats/occupancy", params={"bucket": "fortnight"})
    assert refused.status_code == 422
    assert error(refused)["code"] == "unknown_bucket"


def test_an_empty_window_and_a_year_of_history_are_both_refused(api, seeded):
    backwards = api.get(
        "/stats/duration",
        params={
            "from": datetime.now(timezone.utc).isoformat(),
            "to": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        },
    )
    assert error(backwards)["code"] == "empty_window"

    wide = api.get(
        "/stats/duration",
        params={"from": (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()},
    )
    assert wide.status_code == 422
    assert error(wide)["code"] == "window_too_wide"
    assert "92" in error(wide)["message"]


def test_the_average_stay_comes_with_its_denominator(api, seeded):
    body = api.get("/stats/duration").json()
    # Two closed visits with a measured length; the orphan's NULL is not a zero.
    assert body["sample_size"] == 2
    assert body["average_s"] == pytest.approx(2700.0)
    assert body["median_s"] == pytest.approx(2700.0)
    assert body["longest_s"] == pytest.approx(3600.0)
    assert body["arrivals_considered"] == 3
    assert all(set(hour) == {"hour_of_day", "arrivals"} for hour in body["peak_hours"])


def test_the_doc_s_revenue_endpoint_is_refused_instead_of_answered_with_a_zero(api):
    refused = api.get("/stats/revenue")
    assert refused.status_code == 501
    assert error(refused)["code"] == "not_implemented"
    assert "tariffs" in error(refused)["message"]


# -- the audit trail ----------------------------------------------------------


@pytest.mark.parametrize("role", ["OPERATOR", "VIEWER", "CUSTOMER"])
def test_the_trail_is_an_admin_question(api, seeded, role):
    refused = api.get("/audit-logs", role=role)
    assert refused.status_code == 403
    assert "ADMIN" in error(refused)["message"]


def test_the_trail_is_newest_first_and_filterable_the_way_an_operator_asks(api, seeded):
    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        for index in range(3):
            repos.audit.append("login", actor_email="board@lot.example", detail={"i": index})
    page = api.get("/audit-logs", params={"actor_email": "board@lot.example"}).json()
    # The `tokens` fixture's own login is the oldest of these four, so it is last.
    assert [item["detail"]["i"] for item in page["items"][:3]] == [2, 1, 0]
    assert page["items"][3]["detail"] == {"role": "VIEWER"}
    assert page["total"] == len(page["items"])
    assert len({item["actor_email"] for item in page["items"]}) == 1
    assert api.get("/audit-logs", params={"action": "plate_corrected"}).json()["total"] == 0


def test_a_filter_on_an_action_the_table_cannot_hold_is_refused(api, seeded):
    refused = api.get("/audit-logs", params={"action": "money_stolen"})
    assert refused.status_code == 422
    assert error(refused)["code"] == "unknown_action"


def test_an_audit_row_never_carries_a_password_even_when_the_request_did(api, seeded):
    api.post("/auth/login", role=None, json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"})
    api.post("/auth/login", role=None, json={"email": "desk@lot.example", "password": "wrong-on-purpose"})
    for row in api.get("/audit-logs", params={"limit": 50}).json()["items"]:
        assert "Barrier-Ticket-2026" not in str(row)
        assert "wrong-on-purpose" not in str(row)
        assert "hash" not in str(row["detail"])


# -- health and the error envelope --------------------------------------------


def test_health_answers_about_the_database_the_gpu_and_the_streams(api, seeded):
    body = api.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["anpr_enabled"] == api.ctx.cfg.anpr.enabled
    assert isinstance(body["gpu"]["available"], bool)
    # This fixture starts no pipelines, so there is no camera to be quiet about:
    # `degraded` is about a stream that should exist, not one never asked for.
    assert body["cameras"] == []
    assert body["ws_clients"] == 0
    assert body["dropped_events"] == 0
    # A socket that cannot redeem a ticket is a clock or a reconnect loop, and the
    # two look identical from the browser. This is the number that says which.
    assert body["ws_ticket_refusals"] == 0
    assert body["uptime_s"] >= 0


def test_health_reports_a_missing_migration_by_name_instead_of_raising(api, seeded):
    with api.ctx.database.engine.begin() as connection:
        connection.execute(text("DROP TABLE detection_logs"))
    body = api.get("/health").json()
    assert body["status"] == "degraded"
    assert body["database"] == "migrations pending: detection_logs"


def test_health_is_authenticated_because_it_maps_the_machine(api, seeded):
    anonymous = api.get("/health", role=None)
    assert anonymous.status_code == 401
    assert error(anonymous)["code"] == "token_missing"


def test_a_refused_stub_is_still_a_route_so_it_still_takes_a_token(api):
    """501 is an answer, and a stub that answers to nobody is a map of the system.

    The rule this package states for itself is that only the two token-issuing
    routes are anonymous; a route added later that raises before it checks the
    header is the way that rule quietly stops being true.
    """
    anonymous = api.client.request("GET", api.prefix + "/stats/revenue")
    assert anonymous.status_code == 401
    assert error(anonymous)["code"] == "token_missing"


def test_the_only_routes_that_answer_without_a_token_are_the_two_that_issue_one(api):
    """Every mounted REST route, checked for a token dependency, transitively.

    Read from the app's own dependency graph rather than from the source text, so a
    route that authenticates through `require_roles(...)` counts - that
    dependency's own graph ends at `current_user`. WebSockets are excluded because
    a socket is not reachable without a handshake the router itself refuses
    (`test_api_live.py` asserts both close codes).

    The point is the class, not the instance: a route that answers - even with a
    501 - tells a caller that a path exists, so a stub is not an exception to the
    rule that only the token-issuing routes are anonymous.
    """
    authenticators = {deps.current_user, deps.viewer_user}

    def authenticates(route) -> bool:
        stack = [route.dependant]
        seen: set = set()
        while stack:
            dependant = stack.pop()
            if dependant.call in seen:
                continue
            seen.add(dependant.call)
            if dependant.call in authenticators:
                return True
            stack.extend(dependant.dependencies)
        return False

    rest = rest_routes(api.client.app)
    prefix = api.ctx.cfg.api.prefix
    assert len(rest) > 20, f"found {len(rest)} REST routes - the shape of `app.routes` changed"
    anonymous = sorted(path for path, route in rest if not authenticates(route))
    assert anonymous == [prefix + "/auth/login", prefix + "/auth/refresh"]


def test_a_valid_token_for_a_deactivated_account_is_refused_before_the_payload(api, seeded):
    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        repos.users.deactivate(repos.users.by_email("board@lot.example").id)
    refused = api.get("/health", role="VIEWER")
    assert refused.status_code == 401
    assert error(refused)["code"] == "user_inactive"


def test_every_refusal_has_the_same_two_field_body(api, seeded):
    for response in (
        api.get("/sessions/987654"),
        api.get("/plates/search", params={"q": "x"}),
        api.get("/audit-logs", role="VIEWER"),
        api.get("/cameras/nope"),
    ):
        assert set(response.json()) == {"error"}
        assert set(response.json()["error"]) == {"code", "message"}


def test_a_route_that_does_not_exist_is_the_same_envelope_not_an_html_page(api):
    missing = api.get("/nothing/here")
    assert missing.status_code == 404
    assert error(missing) == {"code": "route_unknown", "message": "no GET /api/v1/nothing/here"}


def test_the_api_is_mounted_under_the_configured_prefix_and_nowhere_else(api, seeded):
    assert api.get("/health").status_code == 200
    assert api.client.get("/health").status_code == 404
    assert api.client.get(api.prefix + "/auth/login").status_code == 405


# -- the lifespan, with a persistence worker ----------------------------------


def test_a_persisting_app_writes_the_visit_that_was_still_on_the_bus_when_it_stopped(
    api_config, api_database, seeded, bus
):
    """`--persist` gives the lifespan a thread it owns, and the guarantee an
    operator actually needs is about the tail: `worker.stop()` joins and then drains
    again, so a visit published just before the stop button is in the database after
    it. The seeded rows are the control - the count moves by exactly the one visit."""
    from fastapi.testclient import TestClient

    from smart_parking.api.app import create_app
    from test_persistence import one_visit

    from conftest import TEST_SECRET

    normalise = plate_normaliser_for(api_config)
    app = create_app(
        api_config, api_database, bus=bus, secret=TEST_SECRET, persist=True, start_pipelines=False
    )
    with api_database.scope() as session:
        before = {row.id for row in Repositories(session, normalise=normalise).sessions.all()}

    with TestClient(app):
        for event in one_visit():
            bus.publish(event)

    with api_database.scope() as session:
        fresh = [
            row
            for row in Repositories(session, normalise=normalise).sessions.all()
            if row.id not in before
        ]
    assert len(fresh) == 1, "the visit on the bus at shutdown is not a lost tail"
    assert (fresh[0].camera_id, fresh[0].status) == ("gate_cam1", SessionStatus.COMPLETED)
    assert fresh[0].duration_seconds == pytest.approx(80.0)
