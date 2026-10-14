"""Phase 7 end to end: what Phase 6 printed is what the database holds.

This is the spec's integration test - `SessionService` events -> persistence
service -> repository -> SQLite -> read back and compare - plus the two structural
promises of the phase: the AI layers still import nothing from `db/`, and a run's
last visit is not lost when the worker shuts down.

Time is the camera's clock throughout, exactly as a replay delivers it: a fixed
origin plus offsets, so every datetime assertion is against an exact value rather
than "about now". The origin is a timezone-aware instant turned into epoch seconds
because that is the one place the two clock styles meet, and the round trip has to
be shown to be lossless.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from re import MULTILINE, search

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from smart_parking.core.config import AppConfig, CameraConfig, DatabaseConfig, GateConfig
from smart_parking.core.events import InMemoryEventBus, RecordingEventBus
from smart_parking.core.schemas import (
    NO_ENTRY,
    PLATE_READ,
    SLOT_FREED,
    SLOT_OCCUPIED,
    SUMMARY,
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    VIOLATION,
    CameraRole,
    Event,
    GateCrossing,
    PlateResult,
    SessionStatus,
    SlotStatus,
)
from smart_parking.db.base import to_utc
from smart_parking.db.models import Vehicle
from smart_parking.db.repositories import Repositories
from smart_parking.services.persistence import (
    PersistenceWorker,
    apply_events,
    format_failures,
    plate_normaliser_for,
    sync_configuration,
)
from smart_parking.services.session_service import DUPLICATE_PLATE, SessionService

ROOT = Path(__file__).resolve().parents[2]
CAM = "gate_cam1"
LOT = "lot_cam1"
PLATE = "MH14AB1234"

ORIGIN = datetime(2026, 1, 12, 9, 0, 0, tzinfo=timezone.utc).timestamp()
# The same instant as the schema stores it: naive UTC.
NINE_OCLOCK = datetime(2026, 1, 12, 9, 0, 0)


def at(seconds: float) -> float:
    """The camera's clock: epoch seconds, as an event carries them."""
    return ORIGIN + seconds


def saved(seconds: float) -> datetime:
    """What the instant `at(seconds)` becomes in a column."""
    return to_utc(at(seconds))


GATE_CAMERA = CameraConfig(
    id=CAM,
    name="Gate / entrance camera",
    role=CameraRole.GATE_ENTRY,
    source="0",
    gate_file="configs/gates/gate_a.json",
)
LOT_CAMERA = CameraConfig(
    id=LOT,
    name="Lot camera 1",
    role=CameraRole.LOT,
    source="0",
    slots_file="configs/slots/lot_a_cam1.json",
)
CFG = AppConfig(cameras=[GATE_CAMERA, LOT_CAMERA], project_root=ROOT)
# The ANPR stage's own normaliser, injected exactly as the CLI injects it.
NORMALISE = plate_normaliser_for(CFG)


@pytest.fixture()
def database(memory_database):
    """A database with its cameras and calibrated bays already seeded."""
    sync_configuration(memory_database, CFG, normalise=NORMALISE)
    return memory_database


@pytest.fixture()
def worker_database(file_database):
    """The seeded database on a file: the shape a threaded run uses."""
    sync_configuration(file_database, CFG, normalise=NORMALISE)
    return file_database


def gate_service(**gate_overrides) -> tuple[SessionService, RecordingEventBus]:
    bus = RecordingEventBus()
    return SessionService(GateConfig(**gate_overrides), bus=bus), bus


def entry(track_id: int = 7, camera: str = CAM, when: float = at(0.0)) -> GateCrossing:
    return GateCrossing(
        camera_id=camera, track_id=track_id, line="entry", role="ENTRY",
        timestamp=when, direction="positive", centroid=(300.0, 405.0),
    )


def exit_(track_id: int = 7, camera: str = CAM, when: float = at(80.0), dwell: bool = False) -> GateCrossing:
    return GateCrossing(
        camera_id=camera, track_id=track_id,
        line="zone_dwell" if dwell else "exit", role="EXIT",
        timestamp=when, direction="dwell" if dwell else "negative",
        centroid=(300.0, 205.0),
    )


def read(
    track_id: int = 7,
    plate: str = PLATE,
    ocr: float = 0.94,
    when: float = at(1.0),
    cls: str = "car",
) -> PlateResult:
    return PlateResult(
        track_id=track_id, plate_text=plate, plate_confidence=0.7, ocr_confidence=ocr,
        timestamp=when, bbox=(240.0, 380.0, 360.0, 410.0),
        stable=True, observations=4, agreement=0.8, class_name=cls,
    )


def plate_event(result: PlateResult, camera: str = CAM) -> Event:
    """The event the ANPR stage publishes alongside the session ones.

    A `SessionService` only *binds* a plate; `AnprPipeline` is what announces it on
    the bus. Building the same event here is what lets a plate read arrive after the
    entry that created the session row, which is the order a real gate produces.
    """
    return Event(
        type=PLATE_READ, camera_id=camera, timestamp=result.timestamp,
        payload=result.to_dict(),
    )


def slot_event(
    status: SlotStatus,
    slot_id: str = "A1",
    when: float = at(10.0),
    track_id: int | None = 4,
    camera: str = LOT,
) -> Event:
    return Event(
        type=SLOT_OCCUPIED if status is SlotStatus.OCCUPIED else SLOT_FREED,
        camera_id=camera,
        timestamp=when,
        payload={
            "slot_id": slot_id,
            "status": status.value,
            "track_id": track_id if status is SlotStatus.OCCUPIED else None,
            "score": 0.58 if status is SlotStatus.OCCUPIED else 0.0,
        },
    )


def one_visit(**gate_overrides) -> list[Event]:
    """One clean gate visit as one camera's bus carried it: ENTER, PLATE_READ, EXIT."""
    sessions, bus = gate_service(**gate_overrides)
    sessions.crossing(entry())
    result = read()
    bus.publish(plate_event(result))
    sessions.plate(result, CAM)
    sessions.crossing(exit_())
    return bus.recorded


def two_visits(**gate_overrides) -> list[Event]:
    """Two visits from one service, so the session ids the rows key on differ."""
    sessions, bus = gate_service(**gate_overrides)
    for track_id, entered_at, exited_at in ((7, 0.0, 80.0), (9, 200.0, 260.0)):
        sessions.crossing(entry(track_id=track_id, when=at(entered_at)))
        result = read(track_id=track_id, when=at(entered_at + 1.0))
        bus.publish(plate_event(result))
        sessions.plate(result, CAM)
        sessions.crossing(exit_(track_id=track_id, when=at(exited_at)))
    return bus.recorded


def entry_event(track_id: int = 7, when: float = 0.0) -> Event:
    sessions, _bus = gate_service()
    return sessions.crossing(entry(track_id=track_id, when=at(when)))[0]


def ghost_entry_event() -> Event:
    """An entry from a camera no configuration declares: its foreign key cannot
    resolve, which is how these tests get a write the database refuses."""
    return Event(
        type=VEHICLE_ENTERED,
        camera_id="gate_cam9",
        timestamp=at(0.0),
        payload={
            "session_id": "gate_cam9-00001",
            "camera_id": "gate_cam9",
            "track_id": 3,
            "status": SessionStatus.ACTIVE.value,
            "entry_ts": at(0.0),
        },
    )


def write(database, events) -> int:
    return apply_events(database, events, normalise=NORMALISE)


# -- the configuration rows -------------------------------------------------
def test_the_configured_cameras_and_bays_are_written_before_any_observation(database):
    """Sessions and occupancy rows point at these by foreign key, so they have to
    exist before an observation does - and re-running startup must not duplicate
    them."""
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert [c.camera_id for c in repos.cameras.all()] == [CAM, LOT]
        gate = repos.cameras.get(CAM)
        assert (gate.role, gate.gate_file) == (CameraRole.GATE_ENTRY, "configs/gates/gate_a.json")
        bays = repos.slots.for_camera(LOT)
        assert [b.slot_code for b in bays] == ["A1", "A2", "B1", "B2", "B3"]
        # Geometry comes from the calibration file the engine reads, not a copy.
        assert len(bays[0].polygon) == 4
        assert all(b.current_status is SlotStatus.UNKNOWN for b in bays)

    assert sync_configuration(database, CFG, normalise=NORMALISE) == 5
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert len(repos.cameras.all()) == 2
        assert len(repos.slots.for_camera(LOT)) == 5


def test_a_camera_whose_calibration_file_is_missing_syncs_its_row_and_no_bays(memory_database):
    """A typo in `slots_file` is a warning and zero bays, not a crash: the gate
    camera has no bays to write either, and it still has to be persistable."""
    cfg = AppConfig(
        cameras=[CameraConfig(id="lot_cam9", role=CameraRole.LOT, source="0",
                              slots_file="configs/slots/not_drawn_yet.json")],
        project_root=ROOT,
    )
    assert sync_configuration(memory_database, cfg, normalise=plate_normaliser_for(cfg)) == 0
    with memory_database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert repos.cameras.get("lot_cam9") is not None
        assert repos.slots.for_camera("lot_cam9") == []


# -- the integration test ----------------------------------------------------
def test_a_visit_phase_six_printed_reads_back_from_sqlite_unchanged(database):
    """ENTER, plate, EXIT -> one completed session row whose every field is the
    value the domain already reported, read back in a fresh unit of work."""
    events = one_visit()
    assert [e.type for e in events] == [VEHICLE_ENTERED, PLATE_READ, VEHICLE_EXITED]
    assert write(database, events) == 3

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        rows = repos.sessions.all()
        assert len(rows) == 1
        row = rows[0]
        # The id the state machine minted, so this row *is* that console line.
        assert row.source_session_id == "gate_cam1-00001"
        assert row.status is SessionStatus.COMPLETED
        assert (row.entry_time, row.exit_time) == (NINE_OCLOCK, saved(80.0))
        # A duration the gate measured in video time, never recomputed here.
        assert row.duration_seconds == 80.0
        assert (row.entry_track_id, row.exit_track_id) == (7, 7)
        assert row.exit_reason == "line"
        assert row.ocr_confidence == 0.94
        # The plate as the Phase 5 validator writes it - one key for two spellings.
        assert row.plate.plate_text == PLATE == NORMALISE("MH-14-AB-1234")
        assert row.vehicle.vehicle_type == "car"
        assert row.review_reason is None and row.anomaly_reason is None
        assert row.slot_id is None  # nothing on this machine can honestly name a bay
        assert row.camera.role is CameraRole.GATE_ENTRY
        assert row.entry_camera_id == row.exit_camera_id == CAM

        # Every event, with its payload intact, and the visit it belongs to.
        assert repos.events.total() == 3
        logged = repos.events.for_session(row.id)
        assert [l.event_type for l in logged] == [VEHICLE_ENTERED, PLATE_READ, VEHICLE_EXITED]
        assert logged[2].payload["duration"] == 80.0
        assert logged[2].occurred_at == saved(80.0)
        assert repos.cameras.get(CAM).last_seen_at == saved(80.0)


def test_a_plate_read_after_the_entry_lands_on_the_visit_it_belongs_to(database):
    """The usual gate order: the vehicle crosses in, the plate becomes legible a
    second later, and the one row already open receives it."""
    assert write(database, [entry_event()]) == 1

    result = read()
    assert write(database, [plate_event(result)]) == 1

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        row = repos.sessions.get("gate_cam1-00001")
        assert row.status is SessionStatus.ACTIVE
        assert row.plate.plate_text == PLATE
        assert row.vehicle.plate_id == row.plate.id
        assert repos.plates.all() == [row.plate]
        # Still one visit: a plate arriving is not a second vehicle.
        assert len(repos.sessions.all()) == 1


def test_a_plate_read_before_the_line_is_kept_without_inventing_a_session(database):
    """A legible plate on a vehicle that never crossed is an observation, not a
    visit. The plate is kept; which visit it belongs to stays unanswered."""
    assert write(database, [plate_event(read())]) == 1
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert repos.sessions.all() == []
        plate = repos.plates.find(PLATE)
        assert plate.reads == 1
        assert repos.vehicles.for_plate(plate)[0].vehicle_type == "car"
        log = repos.events.for_camera(CAM)[0]
        assert (log.event_type, log.session_id) == (PLATE_READ, None)


def test_a_session_exists_with_no_plate_at_all(database):
    """ANPR is not field validated, so a plate-less visit is the normal case. It
    is stored as a session with `plate_id` NULL and a review reason - never with a
    number invented to fill the column."""
    sessions, bus = gate_service()
    sessions.crossing(entry())
    sessions.crossing(exit_(when=at(45.0)))
    write(database, bus.recorded)

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        row = repos.sessions.get("gate_cam1-00001")
        assert (row.plate_id, row.vehicle_id) == (None, None)
        assert row.status is SessionStatus.NEEDS_REVIEW
        assert row.review_reason == "no plate read"
        assert row.duration_seconds == 45.0
        assert repos.plates.all() == []
        assert session.scalar(select(func.count()).select_from(Vehicle)) == 0


def test_a_plate_the_ocr_stage_never_agreed_on_leaves_the_visit_without_one(database):
    """No stable read means no PLATE_READ event at all, and an absence in the
    database is the honest answer - not a low-confidence number to average later."""
    sessions, bus = gate_service()
    sessions.crossing(entry())
    sessions.crossing(exit_())
    write(database, bus.recorded)
    with database.scope() as session:
        row = Repositories(session, normalise=NORMALISE).sessions.get("gate_cam1-00001")
        assert (row.plate_id, row.ocr_confidence, row.vehicle_id) == (None, None, None)


def test_a_lost_plate_read_costs_the_vehicle_row_not_the_identity(database):
    """A plate read that never reached the bus is not a lost car: the session
    event carries the plate the visit ended with, so the identity survives. The
    vehicle row needs the class the *detector* reported, which no session event
    has, so it stays absent rather than guessed."""
    sessions, bus = gate_service()
    sessions.crossing(entry())
    sessions.plate(read(), CAM)
    sessions.crossing(exit_())
    assert write(database, [e for e in bus.recorded if e.type != PLATE_READ]) == 2

    with database.scope() as session:
        row = Repositories(session, normalise=NORMALISE).sessions.get("gate_cam1-00001")
        assert row.plate.plate_text == PLATE
        assert (row.vehicle_id, row.plate_confidence) == (None, None)
        assert row.ocr_confidence == 0.94


def test_an_exit_the_camera_saw_first_is_an_anomaly_with_no_entry_time(database):
    """The doc's exit-without-entry: the half that was measured is stored, the
    half that was not stays NULL, and no duration is derived from the difference."""
    sessions, bus = gate_service()
    events = sessions.crossing(exit_(when=at(30.0)))
    assert [e.type for e in events] == [VIOLATION]
    assert events[0].payload["kind"] == NO_ENTRY
    write(database, events)

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        row = repos.sessions.get("gate_cam1-00001")
        assert (row.entry_time, row.duration_seconds) == (None, None)
        assert (row.status, row.anomaly_reason) == (SessionStatus.ANOMALY, NO_ENTRY)
        assert row.exit_time == saved(30.0)
        assert row.exit_reason == "line"


def test_an_exit_event_that_arrived_without_its_entry_is_not_given_an_entry_time(database):
    """A worker started mid-run, or a bus that overflowed: the entry never reached
    the database, so the exit cannot close a visit that does not exist. It becomes
    the same anomaly rather than a fabricated duration."""
    exit_event = [e for e in one_visit() if e.type == VEHICLE_EXITED][0]
    assert write(database, [exit_event]) == 1

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        row = repos.sessions.get(exit_event.payload["session_id"])
        assert (row.entry_time, row.duration_seconds) == (None, None)
        assert (row.status, row.anomaly_reason) == (SessionStatus.ANOMALY, NO_ENTRY)
        # The plate was in the event, so it is still a real observation.
        assert row.plate.plate_text == PLATE


def test_two_live_sessions_for_one_plate_keep_both_visits_and_flag_the_newcomer(database):
    """The domain's rule survives the round trip: neither record is dropped, the
    second is an anomaly, and the partial unique index is never tripped - because
    only one of the two is still ACTIVE."""
    sessions, bus = gate_service()
    sessions.plate(read(track_id=7), CAM)
    sessions.crossing(entry(track_id=7))
    sessions.plate(read(track_id=8), CAM)
    sessions.crossing(entry(track_id=8, when=at(5.0)))
    assert bus.recorded[-1].type == VIOLATION
    assert bus.recorded[-1].payload["kind"] == DUPLICATE_PLATE
    write(database, bus.recorded)

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        rows = repos.sessions.all()
        assert [r.status for r in rows] == [SessionStatus.ACTIVE, SessionStatus.ANOMALY]
        assert rows[1].anomaly_reason == DUPLICATE_PLATE
        assert rows[0].entry_time == NINE_OCLOCK
        assert rows[1].entry_time == saved(5.0)
        assert rows[0].plate_id == rows[1].plate_id == repos.plates.find(PLATE).id
        assert repos.sessions.active_for_plate(rows[0].plate_id).id == rows[0].id


def test_a_second_visit_by_the_same_car_reuses_the_plate_and_the_vehicle(database):
    """Identity is the plate, not the tracker's number: two visits, one car."""
    assert write(database, two_visits()) == 6

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        rows = repos.sessions.all()
        assert [r.source_session_id for r in rows] == ["gate_cam1-00001", "gate_cam1-00002"]
        assert [r.duration_seconds for r in rows] == [80.0, 60.0]
        assert [r.entry_track_id for r in rows] == [7, 9]
        assert all(r.plate.plate_text == PLATE for r in rows)
        assert all(r.status is SessionStatus.COMPLETED for r in rows)
        assert len(repos.plates.all()) == 1
        assert len(repos.vehicles.all()) == 1
        # A sighting count, not a second car: read on the line and again on exit.
        assert repos.plates.find(PLATE).reads == 4


def test_a_plate_that_changed_mid_visit_is_one_visit_marked_for_review(database):
    """OCR corrected itself. The newest stable read wins, the visit stays one row,
    and the reason is recorded because "which car was this" is exactly the question
    an operator asks about this row."""
    sessions, bus = gate_service()
    sessions.crossing(entry())
    first = read(plate="MH14AB1234")
    bus.publish(plate_event(first))
    sessions.plate(first, CAM)
    corrected = read(plate="MH14AB1235", when=at(2.0))
    bus.publish(plate_event(corrected))
    sessions.plate(corrected, CAM)
    sessions.crossing(exit_())
    write(database, bus.recorded)

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        rows = repos.sessions.all()
        assert len(rows) == 1
        row = rows[0]
        assert row.plate.plate_text == "MH14AB1235"
        assert row.review_reason == "plate changed"
        assert row.status is SessionStatus.NEEDS_REVIEW
        assert row.duration_seconds == 80.0
        assert len(repos.plates.all()) == 2


# -- occupancy --------------------------------------------------------------
def test_a_bay_reports_one_occupancy_interval_per_stay_not_one_per_event(database):
    """Repeated OCCUPIED reports refresh the state; the interval opens once and
    closes on the FSM's own FREE transition."""
    write(database, [
        slot_event(SlotStatus.OCCUPIED, when=at(10.0)),
        slot_event(SlotStatus.OCCUPIED, when=at(20.0)),
        slot_event(SlotStatus.FREE, when=at(600.0)),
    ])
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        slot = repos.slots.find(LOT, "A1")
        assert slot.current_status is SlotStatus.FREE
        assert slot.occupied_by_track_id is None
        assert slot.status_since == saved(600.0)
        intervals = repos.occupancy.history(slot.id)
        assert len(intervals) == 1
        assert (intervals[0].started_at, intervals[0].ended_at) == (saved(10.0), saved(600.0))
        assert (intervals[0].track_id, intervals[0].score) == (4, 0.58)


def test_a_bay_that_is_still_taken_has_an_open_interval_and_no_history_claim(database):
    """`ended_at IS NULL` means occupied right now - a live fact, so nothing here
    closes it on a timer and nothing invents a stay length."""
    write(database, [slot_event(SlotStatus.OCCUPIED)])
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        slot = repos.slots.find(LOT, "A1")
        assert slot.current_status is SlotStatus.OCCUPIED
        assert slot.occupied_by_track_id == 4
        interval = repos.occupancy.open_for(slot.id)
        assert (interval.started_at, interval.ended_at) == (saved(10.0), None)
        assert repos.occupancy.history(slot.id) == [interval]


def test_a_free_transition_for_a_bay_that_never_committed_closes_nothing(database):
    """A camera that started mid-stay, or a bay whose OCCUPIED report was lost, has
    no interval to end. The status is still the observation worth keeping."""
    write(database, [slot_event(SlotStatus.FREE, when=at(60.0))])
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        slot = repos.slots.find(LOT, "A1")
        assert slot.current_status is SlotStatus.FREE
        assert slot.status_since == saved(60.0)
        assert repos.occupancy.history(slot.id) == []


def test_two_stays_in_one_bay_are_two_intervals(database):
    """The history table answers "how long was A1 taken today", so a bay that
    turns over twice has two closed rows, in order, and no third."""
    write(database, [
        slot_event(SlotStatus.OCCUPIED, slot_id="B2", when=at(10.0), track_id=4),
        slot_event(SlotStatus.FREE, slot_id="B2", when=at(100.0)),
        slot_event(SlotStatus.OCCUPIED, slot_id="B2", when=at(200.0), track_id=6),
        slot_event(SlotStatus.FREE, slot_id="B2", when=at(300.0)),
    ])
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        slot = repos.slots.find(LOT, "B2")
        intervals = repos.occupancy.history(slot.id)
        assert [(i.started_at, i.ended_at) for i in intervals] == [
            (saved(10.0), saved(100.0)),
            (saved(200.0), saved(300.0)),
        ]
        assert [i.track_id for i in intervals] == [4, 6]
        assert repos.occupancy.open_for(slot.id) is None


def test_a_slot_event_from_a_camera_that_sees_no_bays_writes_no_slot_row(database):
    """A gate camera has no calibrated bays, so a slot event from one is not a row
    to write - and it is still kept in the log with everything else."""
    assert write(database, [slot_event(SlotStatus.OCCUPIED, camera=CAM)]) == 1
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert repos.slots.for_camera(CAM) == []
        log = repos.events.for_camera(CAM)[0]
        assert (log.payload["slot_id"], log.session_id) == ("A1", None)


def test_the_event_log_holds_every_transition_and_skips_only_the_per_frame_snapshot(database):
    """`SUMMARY` is a counter snapshot the occupancy engine publishes on every
    frame; one row per frame is the one thing this table must not become."""
    events = one_visit() + [
        Event(type=SUMMARY, camera_id=LOT, timestamp=at(3.0),
              payload={"total": 5, "occupied": 1, "available": 4, "unknown": 0}),
        slot_event(SlotStatus.OCCUPIED),
    ]
    assert write(database, events) == len(events)
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        # A SUMMARY is still consumed and counted; it just is not written.
        assert repos.events.total() == 4
        assert repos.events.count_by_type() == {
            VEHICLE_ENTERED: 1, PLATE_READ: 1, VEHICLE_EXITED: 1, SLOT_OCCUPIED: 1,
        }


# -- the worker -------------------------------------------------------------
def test_the_worker_writes_everything_the_bus_carried_before_stop_returns(worker_database):
    """The pipeline never waits for the database, and the run's last visit is
    still in it when `stop()` returns."""
    bus = InMemoryEventBus()
    worker = PersistenceWorker(worker_database, CFG, bus, normalise=NORMALISE, batch_size=2)
    worker.start()
    events = two_visits()
    for event in events:
        bus.publish(event)
    stats = worker.stop()

    assert stats["failures"] == 0 and worker.failures == []
    assert stats["dropped_by_bus"] == 0
    assert stats["events_written"] == len(events)
    assert stats["batches"] >= len(events) // 2

    with worker_database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        rows = repos.sessions.all()
        assert [r.duration_seconds for r in rows] == [80.0, 60.0]
        assert repos.events.total() == len(events)


def test_stop_writes_the_events_a_thread_that_never_ran_left_behind(database):
    """The guarantee belongs to `stop()`, not to the thread: draining after the
    join is what keeps a run from losing the sessions still on the bus."""
    bus = InMemoryEventBus()
    worker = PersistenceWorker(database, CFG, bus, normalise=NORMALISE)
    assert worker._thread is None
    for event in one_visit():
        bus.publish(event)
    stats = worker.stop()
    assert (stats["events_written"], stats["failures"], stats["batches"]) == (3, 0, 1)
    with database.scope() as session:
        assert len(Repositories(session, normalise=NORMALISE).sessions.all()) == 1


def test_one_event_that_cannot_be_written_does_not_take_the_others_down(worker_database):
    """A camera that is not in the config fails its foreign key. The batch is
    retried one event at a time, the error is recorded and surfaced, and the good
    visit is still in the database - which is the opposite of swallowing."""
    bus = InMemoryEventBus()
    worker = PersistenceWorker(worker_database, CFG, bus, normalise=NORMALISE, batch_size=50)
    worker.start()
    for event in list(one_visit()) + [ghost_entry_event()]:
        bus.publish(event)
    stats = worker.stop()

    assert stats["failures"] == 1 and len(worker.failures) == 1
    etype, error = worker.failures[0]
    assert etype == VEHICLE_ENTERED
    assert "IntegrityError" in error and "gate_cam9" in error
    report = format_failures(worker.failures)
    assert report.startswith("1 event(s) could not be persisted")
    assert "IntegrityError" in report

    with worker_database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert len(repos.sessions.all()) == 1
        assert repos.events.total() == 3


def ghost_events(count: int) -> list[Event]:
    """`count` entries from a camera no configuration declares, each refused by
    the same foreign key - a replayed clip's poison event seen at server scale."""
    return [
        Event(
            type=VEHICLE_ENTERED,
            camera_id="gate_cam9",
            timestamp=at(float(i)),
            payload={
                "session_id": f"gate_cam9-{i:05d}",
                "camera_id": "gate_cam9",
                "track_id": i,
                "status": SessionStatus.ACTIVE.value,
                "entry_ts": at(float(i)),
            },
        )
        for i in range(count)
    ]


def test_the_failure_sample_is_capped_and_the_count_is_not(worker_database):
    """`failures` is a bounded sample because a server runs a shift where a CLI
    runs one clip: one event the database refuses on every frame of a loop would
    otherwise grow a list of tracebacks without limit. Nothing is hidden by that -
    `stats["failures"]` is the true count and the report prints it."""
    bus = InMemoryEventBus()
    worker = PersistenceWorker(
        worker_database, CFG, bus, normalise=NORMALISE, batch_size=50, max_failures_kept=3
    )
    worker.start()
    for event in ghost_events(10):
        bus.publish(event)
    stats = worker.stop()

    assert (stats["failures"], worker.failure_total) == (10, 10)
    assert len(worker.failures) == 3
    assert {etype for etype, _ in worker.failures} == {VEHICLE_ENTERED}
    assert "IntegrityError" in worker.failures[0][1] and "gate_cam9" in worker.failures[0][1]

    report = format_failures(worker.failures, limit=2, total=worker.failure_total)
    assert report.startswith("10 event(s) could not be persisted")
    assert "(+8 more)" in report
    # Without the total the list is all the caller has, and says so.
    assert format_failures(worker.failures, limit=2).startswith("3 event(s)")

    with worker_database.scope() as session:
        assert Repositories(session, normalise=NORMALISE).events.total() == 0


def test_the_worker_reports_what_the_bus_had_to_drop(database):
    """A row count is not a promise of completeness. The bus drops the oldest
    event rather than stall inference, and `dropped_by_bus` is the number that
    tells an operator the sessions may be partial - the visit that lost its entry
    is written as the anomaly it is, never with a duration guessed backwards."""
    bus = InMemoryEventBus(maxsize=1)
    worker = PersistenceWorker(database, CFG, bus, normalise=NORMALISE, batch_size=10)
    events = one_visit()
    for event in events:
        bus.publish(event)
    stats = worker.stop()

    assert stats["dropped_by_bus"] == len(events) - 1
    assert stats["events_written"] == 1 and stats["failures"] == 0

    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        rows = repos.sessions.all()
        assert len(rows) == 1
        assert (rows[0].entry_time, rows[0].duration_seconds) == (None, None)
        assert (rows[0].status, rows[0].anomaly_reason) == (SessionStatus.ANOMALY, NO_ENTRY)


def test_format_failures_lists_the_first_few_and_counts_the_rest():
    assert format_failures([]) == "no persistence failures"
    rows = [(VEHICLE_ENTERED, f"IntegrityError: row {i}") for i in range(7)]
    text = format_failures(rows, limit=2)
    assert text.startswith("7 event(s) could not be persisted")
    assert "(+5 more)" in text
    assert "row 0" in text and "row 1" in text and "row 2" not in text


# -- the unit of work -------------------------------------------------------
def test_a_failed_batch_writes_nothing_at_all(database):
    """An entry, its plate and its vehicle are one transaction: a batch that
    raises leaves no half of a visit behind."""
    with pytest.raises(IntegrityError):
        write(database, [entry_event(), ghost_entry_event()])
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert repos.sessions.all() == []
        assert repos.events.total() == 0


def test_writing_the_same_visit_twice_is_still_one_session_row(database):
    """Replayed events (a rerun of a clip, a redelivered batch) key on the id the
    state machine minted, so the row count stays one either way."""
    events = one_visit()
    write(database, events)
    assert write(database, events) == len(events)
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        rows = repos.sessions.all()
        assert len(rows) == 1
        assert (rows[0].status, rows[0].duration_seconds) == (SessionStatus.COMPLETED, 80.0)
        # The event log is a trail, not a state: each delivery is its own row.
        assert repos.events.total() == 6


def test_an_anomaly_reported_by_the_lot_camera_is_logged_without_a_session(database):
    """A straddling vehicle is a parking violation, not a visit: no session row
    exists to attach it to, and none is borrowed from the nearest one."""
    violation = Event(
        type=VIOLATION, camera_id=LOT, timestamp=at(5.0),
        payload={"kind": "DOUBLE_PARK", "track_id": 4, "slots": ["A1", "A2"], "severity": "MEDIUM"},
    )
    assert write(database, [violation]) == 1
    with database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        assert repos.sessions.all() == []
        log = repos.events.for_camera(LOT)[0]
        assert (log.event_type, log.session_id, log.payload["slots"]) == (VIOLATION, None, ["A1", "A2"])


def test_a_burst_of_frames_is_fewer_transactions_than_events(worker_database):
    """The lot camera publishes a SUMMARY every frame, and a SUMMARY writes no
    row. Committing one of them per frame would make the database the bottleneck
    of an empty queue, so the worker waits a moment for a batch to fill: a run of
    30 frame events costs a couple of transactions, not thirty."""
    bus = InMemoryEventBus()
    worker = PersistenceWorker(worker_database, CFG, bus, normalise=NORMALISE)
    worker.start()
    events = [
        Event(type=SUMMARY, camera_id=LOT, timestamp=at(1.0 + i * 0.05),
              payload={"total": 5, "occupied": 0, "available": 5, "unknown": 0})
        for i in range(30)
    ]
    for event in events:
        bus.publish(event)
    stats = worker.stop()

    assert stats["events_written"] == len(events) and stats["failures"] == 0
    assert stats["batches"] < len(events)
    with worker_database.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        # Nothing was invented and nothing was logged: the only write a summary
        # burst makes is the camera's last-seen timestamp.
        assert repos.events.total() == 0
        assert repos.cameras.get(LOT).last_seen_at == saved(1.0 + 29 * 0.05)


# -- the seam this phase must not break -------------------------------------
def test_the_ai_layers_still_import_nothing_from_the_database():
    """`services/persistence.py` is the only module that knows both halves. If a
    detector, a tracker or a pipeline started importing SQLAlchemy this test fails,
    which is the entire point of writing it."""
    root = ROOT / "src" / "smart_parking"
    pattern = r"^\s*(import|from)\s+(smart_parking\.db|sqlalchemy|alembic)"
    checked = [root / package for package in ("core", "detection", "parking", "tracking", "anpr", "pipeline", "video")]
    checked_files = [p for d in checked for p in sorted(d.rglob("*.py"))]
    checked_files.append(root / "services" / "session_service.py")
    offenders = [
        str(path.relative_to(root))
        for path in checked_files
        if search(pattern, path.read_text(encoding="utf-8"), MULTILINE)
    ]
    assert offenders == []
