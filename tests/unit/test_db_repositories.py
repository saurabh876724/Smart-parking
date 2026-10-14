"""Phase 7 repositories: the writes the persistence layer is judged on.

Every test here is a rule the Phase 7 spec states, checked one unit of work at a
time against a real SQLite database: an entry then an exit is one completed
session with the duration the gate measured; a duplicate entry is the same row;
an exit with no entry keeps no entry time; a visit exists whether or not ANPR
read anything; and a failure rolls the whole unit of work back.

Nothing here goes through the pipeline - these are the repository's own
promises. `test_persistence.py` is where the events become these calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from smart_parking.anpr.validation import PlateValidator
from smart_parking.core.config import AnprValidationConfig, CameraConfig
from smart_parking.core.schemas import (
    PLATE_READ,
    SLOT_FREED,
    SLOT_OCCUPIED,
    SUMMARY,
    VEHICLE_ENTERED,
    VIOLATION,
    CameraRole,
    Event,
    SessionStatus,
    SlotStatus,
    SlotType,
)
from smart_parking.db.models import LicensePlate, ParkingSession, Vehicle
from smart_parking.db.repositories import Repositories

# A fixed clock, so an assertion can compare datetimes exactly rather than
# "roughly now". The values are epoch-free: only the offsets matter.
T0 = datetime(2026, 1, 12, 9, 0, 0)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# The Phase 5 validator, built once with its default config: the same rule the
# ANPR voting stage used, injected rather than copied.
VALIDATOR = PlateValidator(AnprValidationConfig())


def normalise(text: str) -> str:
    return VALIDATOR.normalise(text)


def camera(session, camera_id: str = "gate_cam1", **fields):
    return Repositories(session, normalise=normalise).cameras.get_or_create(
        camera_id, name=fields.pop("name", camera_id), role=fields.pop("role", CameraRole.GATE_ENTRY),
        source=fields.pop("source", "0"), **fields
    )


# -- cameras and slots: the configured world --------------------------------
def test_cameras_are_the_configured_ones(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        repos.cameras.sync_from_config(
            [
                CameraConfig(id="lot_a_cam1", name="Lot A", role=CameraRole.LOT,
                             source="data/lot.mp4", slots_file="configs/slots/lot_a_cam1.json"),
                CameraConfig(id="gate_cam1", name="Gate 1", role=CameraRole.GATE_ENTRY,
                             source="data/gate.mp4", gate_file="configs/gates/gate_cam1.json"),
            ]
        )
        # A second sync of the same config refreshes, it does not duplicate.
        repos.cameras.sync_from_config(
            [CameraConfig(id="gate_cam1", name="Gate 1 west", role=CameraRole.GATE_ENTRY,
                          source="data/other.mp4")]
        )
        rows = {row.camera_id: row for row in repos.cameras.all()}
        assert set(rows) == {"lot_a_cam1", "gate_cam1"}
        assert rows["gate_cam1"].name == "Gate 1 west"
        assert rows["gate_cam1"].source == "data/other.mp4"
        assert rows["lot_a_cam1"].slots_file == "configs/slots/lot_a_cam1.json"


def test_slot_rows_are_the_calibrated_bays(memory_database):
    """`sync` takes the same shape the calibration tool writes, so a bay in the
    database is the bay an operator drew."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session, "lot_a_cam1", role=CameraRole.LOT)
        slots = repos.slots.sync(
            "lot_a_cam1",
            [
                {"code": "A1", "type": "REGULAR", "polygon": [[0, 0], [10, 0], [10, 20], [0, 20]]},
                {"code": "A2", "type": "EV", "polygon": [[20, 0], [30, 0], [30, 20], [20, 20]]},
            ],
            frame_size={"width": 1920, "height": 1080},
        )
        assert [slot.slot_code for slot in slots] == ["A1", "A2"]
        assert slots[1].slot_type is SlotType.EV
        assert slots[0].polygon[2] == [10, 20]
        assert slots[0].current_status is SlotStatus.UNKNOWN
        # Re-syncing the file is an update of the same two rows.
        again = repos.slots.sync("lot_a_cam1", [{"code": "A1", "polygon": [[0, 0], [11, 0], [11, 20]]}])
        assert len(again) == 1 and again[0].id == slots[0].id
        assert len(repos.slots.for_camera("lot_a_cam1")) == 2


def test_a_slots_status_is_written_once_per_change(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session, "lot_a_cam1", role=CameraRole.LOT)
        slot = repos.slots.sync("lot_a_cam1", [{"code": "A1", "polygon": [[0, 0], [1, 0], [1, 1]]}])[0]
        assert repos.slots.set_status(slot, SlotStatus.OCCUPIED, at(0), track_id=9) is True
        # The same committed state arriving again is not a transition.
        assert repos.slots.set_status(slot, SlotStatus.OCCUPIED, at(5), track_id=9) is False
        assert slot.status_since == at(0)
        assert slot.occupied_by_track_id == 9
        # A camera that stopped reporting is always worth writing, because an
        # operator has to be able to tell "empty" from "unobserved".
        assert repos.slots.set_status(slot, SlotStatus.UNKNOWN, at(7)) is True
        assert slot.occupied_by_track_id is None


def test_occupancy_history_is_one_interval_per_stay(memory_database):
    """A bay occupied for an hour of video is one row, not one per frame."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session, "lot_a_cam1", role=CameraRole.LOT)
        slot = repos.slots.sync("lot_a_cam1", [{"code": "A3", "polygon": [[0, 0], [1, 0], [1, 1]]}])[0]
        repos.occupancy.open_interval(slot, at(0), track_id=4, score=0.62)
        assert repos.occupancy.open_for(slot.id) is not None
        closed = repos.occupancy.close_interval(slot.id, at(900))
        assert closed.ended_at == at(900)
        assert repos.occupancy.open_for(slot.id) is None
        repos.occupancy.open_interval(slot, at(1000), track_id=7)
        assert repos.occupancy.close_interval(slot.id, at(1100)) is not None
        assert len(repos.occupancy.history(slot.id)) == 2
        # A FREE report for a bay that holds nothing closes nothing, and no
        # interval is invented to be closed.
        assert repos.occupancy.close_interval(slot.id, at(1200)) is None


# -- plates and vehicles ----------------------------------------------------
def test_a_plate_is_stored_once_in_its_normalised_form(memory_database):
    """`mh-20-ab-1234`, `MH20AB1234` and `MH 20 AB 1234` are one plate.

    The normaliser is the one the ANPR voting stage used, so the row a session
    points at cannot disagree with the text the pipeline printed.
    """
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        first = repos.plates.record("mh-20-ab-1234", at(0), 0.62)
        second = repos.plates.record("MH 20 AB 1234", at(30), 0.94)
        assert first.id == second.id
        assert first.plate_text == "MH20AB1234"
        # The best score ever seen, not the most recent one.
        assert first.confidence == pytest.approx(0.94)
        assert first.reads == 2
        assert first.first_seen_at == at(0) and first.last_seen_at == at(30)
        assert len(repos.plates.all()) == 1
        assert repos.plates.find("MH20AB1234").id == first.id


def test_a_vehicle_is_one_row_per_plate_and_class(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        plate = repos.plates.record("MH14AB1234", at(0), 0.9)
        car = repos.vehicles.record(plate, "car")
        assert repos.vehicles.record(plate, "car").id == car.id
        bus = repos.vehicles.record(plate, "bus")
        assert bus.id != car.id
        assert [vehicle.vehicle_type for vehicle in repos.vehicles.for_plate(plate)] == ["bus", "car"]


def test_a_vehicle_row_is_never_keyed_on_a_track_id(memory_database):
    """A ByteTrack id is a sighting: the same number means a different car the
    next time the tracker restarts, so identity comes from the plate only."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        plate = repos.plates.record("MH14AB1234", at(0), 0.9)
        vehicle = repos.vehicles.record(plate, "car")
        first = repos.sessions.open("s-1", "gate_cam1", 12, at(0), plate=plate, vehicle=vehicle)
        repos.sessions.close("s-1", at(60), 60.0, "line", exit_track_id=99)
        # Track 12 again, hours later, a different car: nothing about the row
        # links the visits except their own ids.
        second = repos.sessions.open("s-2", "gate_cam1", 12, at(4000))
        assert second.id != first.id
        assert first.vehicle_id == vehicle.id
        assert second.vehicle_id is None
        assert session.scalar(select(func.count()).select_from(Vehicle)) == 1


def test_no_plate_means_no_plate_row(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        row = repos.sessions.open("s-1", "gate_cam1", 3, at(0))
        repos.sessions.close("s-1", at(60), 60.0, "line")
        assert row.plate_id is None
        assert session.scalar(select(func.count()).select_from(LicensePlate)) == 0


# -- the visit lifecycle ----------------------------------------------------
def test_entry_then_exit_is_one_completed_session_with_the_measured_duration(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        opened = repos.sessions.open("gate_cam1-00001", "gate_cam1", 7, at(0))
        assert opened.status is SessionStatus.ACTIVE
        assert opened.entry_time == at(0)
        closed = repos.sessions.close("gate_cam1-00001", at(172.5), 172.5, "dwell", exit_track_id=7)
        assert closed.status is SessionStatus.COMPLETED
        assert closed.duration_seconds == pytest.approx(172.5)
        assert closed.exit_reason == "dwell"
        assert len(repos.sessions.all()) == 1


def test_the_duration_is_the_ones_the_gate_measured(memory_database):
    """`close` stores what it is handed. A repository that recomputed it from the
    two datetimes would quietly redefine a duration as wall-clock time."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        repos.sessions.open("s-1", "gate_cam1", 1, at(1000.0))
        row = repos.sessions.close("s-1", at(1030.0), 29.967, "line")
        assert row.duration_seconds == pytest.approx(29.967)


def test_a_duplicate_entry_event_reuses_the_same_row(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        first = repos.sessions.open("s-1", "gate_cam1", 5, at(0), plate=None)
        again = repos.sessions.open("s-1", "gate_cam1", 5, at(0))
        assert first.id == again.id
        assert len(repos.sessions.all()) == 1


def test_a_repeated_exit_with_the_same_time_changes_nothing(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        repos.sessions.open("s-1", "gate_cam1", 5, at(0))
        repos.sessions.close("s-1", at(40), 40.0, "line")
        again = repos.sessions.close("s-1", at(40), 40.0, "line")
        assert again.status is SessionStatus.COMPLETED
        assert again.duration_seconds == pytest.approx(40.0)


def test_a_second_different_exit_for_one_visit_is_refused(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        repos.sessions.open("s-1", "gate_cam1", 5, at(0))
        repos.sessions.close("s-1", at(40), 40.0, "line")
        with pytest.raises(ValueError, match="already closed"):
            repos.sessions.close("s-1", at(52), 52.0, "dwell")


def test_closing_a_visit_that_was_never_opened_is_an_error(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        with pytest.raises(ValueError, match="no session"):
            repos.sessions.close("s-nope", at(40), 40.0, "line")


def test_an_exit_with_no_entry_keeps_no_entry_time_and_no_duration(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        row = repos.sessions.record_orphan_exit(
            "s-9", "gate_cam1", 8, at(50), "line", "exit_without_entry"
        )
        assert row.status is SessionStatus.ANOMALY
        assert row.entry_time is None
        assert row.duration_seconds is None
        assert row.exit_time == at(50)
        assert row.anomaly_reason == "exit_without_entry"
        # ...and it is idempotent, like every other write keyed on the visit.
        assert repos.sessions.record_orphan_exit(
            "s-9", "gate_cam1", 8, at(50), "line", "exit_without_entry"
        ).id == row.id


def test_a_plate_can_arrive_after_the_entry(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        row = repos.sessions.open("s-1", "gate_cam1", 2, at(0))
        plate = repos.plates.record("DL3CAB1234", at(2), 0.88)
        vehicle = repos.vehicles.record(plate, "car")
        repos.sessions.bind_plate(row, plate, ocr_confidence=0.88, plate_confidence=0.71, vehicle=vehicle)
        assert row.plate_id == plate.id
        assert row.vehicle_id == vehicle.id
        assert row.ocr_confidence == pytest.approx(0.88)
        assert row.review_reason is None


def test_binding_a_different_plate_marks_the_visit_for_review(memory_database):
    """A re-voted plate is trusted, and the change is recorded rather than
    silently applied - this is the visit an operator will be asked about."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        row = repos.sessions.open("s-1", "gate_cam1", 2, at(0))
        repos.sessions.bind_plate(row, repos.plates.record("MH14AA0001", at(1), 0.6))
        repos.sessions.bind_plate(row, repos.plates.record("MH14AA0002", at(4), 0.9))
        assert row.plate.plate_text == "MH14AA0002"
        assert row.review_reason == "plate changed"


def test_binding_a_plate_a_live_visit_holds_makes_the_newcomer_an_anomaly(memory_database):
    """The row-level form of the domain's duplicate-session rule.

    Both visits survive (the doc's rule is that neither is dropped) and the
    unique index is satisfied because only one of them is still ACTIVE.
    """
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        plate = repos.plates.record("MH14AB1234", at(0), 0.9)
        first = repos.sessions.open("s-1", "gate_cam1", 1, at(0), plate=plate)
        second = repos.sessions.open("s-2", "gate_cam1", 2, at(10))
        repos.sessions.bind_plate(second, plate)
        assert first.status is SessionStatus.ACTIVE
        assert second.status is SessionStatus.ANOMALY
        assert second.anomaly_reason == "duplicate_active_session"


def test_the_active_lookups_only_see_live_visits(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        plate = repos.plates.record("MH14AB1234", at(0), 0.9)
        slot = repos.slots.sync("gate_cam1", [{"code": "A1", "polygon": [[0, 0], [1, 0], [1, 1]]}])[0]
        row = repos.sessions.open("s-1", "gate_cam1", 6, at(0), plate=plate)
        repos.sessions.bind_slot(row, slot.id)
        assert repos.sessions.active_for("gate_cam1", 6) is row
        assert repos.sessions.active_for_plate(plate.id) is row
        assert repos.sessions.active_for_slot(slot.id) is row
        repos.sessions.close("s-1", at(60), 60.0, "line")
        assert repos.sessions.active_for("gate_cam1", 6) is None
        assert repos.sessions.active_for_plate(plate.id) is None
        assert repos.sessions.active_for_slot(slot.id) is None


def test_a_needs_review_visit_keeps_its_duration(memory_database):
    """A low-confidence plate still booked the parking; the flag says whom to
    tell, not that the visit did not happen."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        repos.sessions.open("s-1", "gate_cam1", 1, at(0), review_reason="plate confidence 0.42 below 0.80")
        row = repos.sessions.close("s-1", at(60), 60.0, "line", status=SessionStatus.NEEDS_REVIEW)
        assert row.status is SessionStatus.NEEDS_REVIEW
        assert row.duration_seconds == pytest.approx(60.0)
        assert row.review_reason == "plate confidence 0.42 below 0.80"


# -- the event log ----------------------------------------------------------
def test_the_event_log_holds_domain_events_not_frames(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        repos = Repositories(session, normalise=normalise)
        camera(session)
        camera(session, "lot_a_cam1", role=CameraRole.LOT)
        row = repos.sessions.open("s-1", "gate_cam1", 1, at(0))
        events = [
            Event(type=VEHICLE_ENTERED, camera_id="gate_cam1", timestamp=0.0,
                  payload={"session_id": "s-1", "track_id": 1}),
            # 400 summary events would be the per-frame traffic the spec forbids;
            # one is enough to show it is refused.
            Event(type=SUMMARY, camera_id="gate_cam1", timestamp=0.0, payload={"free": 9}),
            Event(type=PLATE_READ, camera_id="gate_cam1", timestamp=1.0,
                  payload={"track_id": 1, "plate": "MH14AB1234", "class": "car"}),
            Event(type=VIOLATION, camera_id="lot_a_cam1", timestamp=2.0,
                  payload={"kind": "DOUBLE_PARK", "track_id": 3, "slots": ["A1", "A2"]}),
        ]
        logged = [
            repos.events.append(events[0], session_row_id=row.id),
            repos.events.append(events[1]),
            repos.events.append(events[2], session_row_id=row.id),
            # The lot camera's violation belongs to no visit: the link stays
            # empty rather than pointing at whatever session was nearby.
            repos.events.append(events[3]),
        ]
        assert [written is None for written in logged] == [False, True, False, False]
        assert repos.events.total() == 3
        assert repos.events.count_by_type() == {
            VEHICLE_ENTERED: 1,
            PLATE_READ: 1,
            VIOLATION: 1,
        }
        assert [event.event_type for event in repos.events.for_session(row.id)] == [
            VEHICLE_ENTERED,
            PLATE_READ,
        ]
        assert repos.events.for_camera("lot_a_cam1")[0].track_id == 3


def test_slot_events_are_logged_with_their_payload(memory_database):
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session, "lot_a_cam1", role=CameraRole.LOT)
        repos.slots.sync("lot_a_cam1", [{"code": "A1", "polygon": [[0, 0], [1, 0], [1, 1]]}])
        repos.events.append(
            Event(type=SLOT_OCCUPIED, camera_id="lot_a_cam1", timestamp=10.0,
                  payload={"slot_id": "A1", "status": "OCCUPIED", "track_id": 4, "score": 0.58})
        )
        repos.events.append(
            Event(type=SLOT_FREED, camera_id="lot_a_cam1", timestamp=70.0,
                  payload={"slot_id": "A1", "status": "FREE", "track_id": None, "score": 0.0})
        )
        rows = repos.events.for_camera("lot_a_cam1")
        assert rows[0].payload["slot_id"] == "A1"
        assert rows[0].vehicle_class is None


# -- the unit of work -------------------------------------------------------
def test_a_repository_never_commits_on_its_own(memory_database):
    """The caller owns the transaction, which is what lets an entry, its plate and
    its vehicle be one atomic write."""
    session = memory_database.session_factory()
    repos = Repositories(session, normalise=normalise)
    camera(session)
    plate = repos.plates.record("MH14AB1234", at(0), 0.9)
    repos.sessions.open("s-1", "gate_cam1", 1, at(0), plate=plate)
    session.close()
    with memory_database.scope() as check:
        assert Repositories(check, normalise=normalise).sessions.get("s-1") is None
        assert Repositories(check, normalise=normalise).plates.all() == []


def test_a_failed_unit_of_work_writes_nothing(memory_database):
    with pytest.raises(RuntimeError):
        with memory_database.scope() as session:
            repos = Repositories(session, normalise=normalise)
            camera(session)
            plate = repos.plates.record("MH14AB1234", at(0), 0.9)
            repos.sessions.open("s-1", "gate_cam1", 1, at(0), plate=plate)
            repos.events.append(
                Event(type=VEHICLE_ENTERED, camera_id="gate_cam1", timestamp=0.0,
                      payload={"session_id": "s-1", "track_id": 1})
            )
            raise RuntimeError("the pipeline died mid-visit")
    with memory_database.scope() as check:
        repos = Repositories(check, normalise=normalise)
        assert repos.sessions.all() == []
        assert repos.plates.all() == []
        assert repos.events.total() == 0


def test_a_visit_row_survives_a_reload(memory_database):
    """Read it back in another session: what was written is in the file, not in
    the identity map."""
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        camera(session)
        plate = repos.plates.record("MH14AB1234", at(0), 0.93)
        vehicle = repos.vehicles.record(plate, "car")
        repos.sessions.open("s-1", "gate_cam1", 1, at(0), plate=plate, vehicle=vehicle,
                            ocr_confidence=0.93)
        repos.sessions.close("s-1", at(120), 120.0, "line", exit_track_id=1)

    with memory_database.scope() as session:
        row = Repositories(session, normalise=normalise).sessions.get("s-1")
        assert isinstance(row, ParkingSession)
        assert row.plate.plate_text == "MH14AB1234"
        assert row.vehicle.vehicle_type == "car"
        assert row.camera.role is CameraRole.GATE_ENTRY
        assert row.duration_seconds == pytest.approx(120.0)
