"""Session state machine (Phase 6, test group: entry/exit lifecycle).

Scripted crossings and plates with known timestamps - no video, no OCR, no
database. Every assertion here is a rule the architecture doc states for the
"Parking Intelligence Engine", checked against the numbers it promises: one
session per visit, a real duration, and an explicit anomaly instead of a
plausible guess.
"""

from __future__ import annotations

from pathlib import Path
from re import MULTILINE, search

import pytest

from smart_parking.core.config import GateConfig
from smart_parking.core.events import RecordingEventBus
from smart_parking.core.schemas import (
    PLATE_READ,
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    VIOLATION,
    GateCrossing,
    PlateResult,
    SessionStatus,
)
from smart_parking.services.session_service import (
    DUPLICATE_PLATE,
    NO_ENTRY,
    UNORDERED_EXIT,
    ParkingSession,
    SessionService,
)

CAM = "gate_cam1"
T0 = 1_000.0
GOOD_PLATE = "MH14AB1234"


def entry(track_id: int = 7, camera: str = CAM, at: float = T0, line: str = "entry") -> GateCrossing:
    return GateCrossing(
        camera_id=camera, track_id=track_id, line=line, role="ENTRY",
        timestamp=at, direction="positive", centroid=(300.0, 405.0),
    )


def exit_(track_id: int = 7, camera: str = CAM, at: float = T0 + 80.0, dwell: bool = False) -> GateCrossing:
    return GateCrossing(
        camera_id=camera, track_id=track_id, line="zone_dwell" if dwell else "exit",
        role="EXIT", timestamp=at, direction="dwell" if dwell else "negative",
        centroid=(300.0, 205.0),
    )


def read(track_id: int = 7, plate: str = GOOD_PLATE, ocr: float = 0.94, at: float = T0) -> PlateResult:
    return PlateResult(
        track_id=track_id, plate_text=plate, plate_confidence=0.7, ocr_confidence=ocr,
        timestamp=at, bbox=(240.0, 380.0, 360.0, 410.0), stable=True,
        observations=4, agreement=0.8, class_name="car",
    )


def service(**gate_overrides) -> tuple[SessionService, RecordingEventBus]:
    bus = RecordingEventBus()
    return SessionService(GateConfig(**gate_overrides), bus=bus), bus


def by_type(bus: RecordingEventBus, etype: str) -> list[dict]:
    return [e.payload for e in bus.recorded if e.type == etype]


# -- a clean visit ------------------------------------------------------------
def test_an_entry_creates_one_active_session():
    sessions, bus = service()
    events = sessions.crossing(entry())
    assert [e.type for e in events] == [VEHICLE_ENTERED]
    assert len(sessions.sessions) == 1
    session = next(iter(sessions.sessions.values()))
    assert session.status is SessionStatus.ACTIVE
    assert session.plate is None
    assert session.duration_s is None
    assert sessions.open_sessions() == [session]


def test_a_full_visit_completes_with_its_duration():
    sessions, bus = service()
    sessions.crossing(entry(at=T0))
    session = next(iter(sessions.sessions.values()))
    sessions.plate(read(at=T0 + 1.0), CAM)
    sessions.crossing(exit_(at=T0 + 80.0))

    assert session.status is SessionStatus.COMPLETED
    assert session.plate == GOOD_PLATE
    assert session.duration_s == pytest.approx(80.0)
    assert session.exit_reason == "line"
    payload = by_type(bus, VEHICLE_EXITED)[0]
    assert payload["duration"] == pytest.approx(80.0)
    assert payload["plate"] == GOOD_PLATE
    assert payload["status"] == "COMPLETED"
    # Closed sessions are no longer waiting for an exit.
    assert sessions.open_sessions() == []


def test_event_timestamps_are_video_time():
    sessions, bus = service()
    sessions.crossing(entry(at=T0))
    assert bus.recorded[0].timestamp == T0


def test_a_plate_read_before_the_line_binds_at_entry():
    """The plate is legible a second or two before the centroid crosses the
    line, so the read arrives first and must not be thrown away."""
    sessions, bus = service()
    assert sessions.plate(read(at=T0 - 1.0), CAM) is None  # parked, no session yet
    sessions.crossing(entry(at=T0))
    payload = by_type(bus, VEHICLE_ENTERED)[0]
    assert payload["plate"] == GOOD_PLATE
    assert payload["ocr_confidence"] == pytest.approx(0.94)


def test_a_plate_read_after_the_entry_binds_to_the_open_session():
    sessions, bus = service()
    sessions.crossing(entry())
    session = sessions.plate(read(), CAM)
    assert isinstance(session, ParkingSession)
    assert session.plate == GOOD_PLATE
    # The service does not re-publish PLATE_READ - the ANPR stage already did.
    assert by_type(bus, PLATE_READ) == []


# -- idempotency -------------------------------------------------------------
def test_a_repeated_entry_for_the_same_track_is_one_session():
    sessions, bus = service()
    sessions.crossing(entry())
    assert sessions.crossing(entry()) == []
    assert sessions.duplicate_crossings == 1
    assert len(sessions.sessions) == 1
    assert len(by_type(bus, VEHICLE_ENTERED)) == 1


def test_a_second_visit_after_an_exit_opens_a_new_session():
    sessions, bus = service()
    sessions.crossing(entry(at=T0))
    sessions.crossing(exit_(at=T0 + 30.0))
    sessions.crossing(entry(at=T0 + 400.0))
    assert len(sessions.sessions) == 2
    assert len(by_type(bus, VEHICLE_ENTERED)) == 2
    assert len(by_type(bus, VEHICLE_EXITED)) == 1


def test_the_same_track_id_on_two_cameras_is_two_sessions():
    sessions, _ = service()
    sessions.crossing(entry(camera=CAM))
    sessions.crossing(entry(camera="gate_cam2"))
    assert len(sessions.open_sessions()) == 2
    assert {s.camera_id for s in sessions.open_sessions()} == {CAM, "gate_cam2"}


# -- dwell exits -------------------------------------------------------------
def test_a_dwell_exit_is_recorded_as_such():
    sessions, bus = service()
    sessions.crossing(entry(at=T0))
    sessions.plate(read(), CAM)
    sessions.crossing(exit_(at=T0 + 20.0, dwell=True))
    session = next(iter(sessions.sessions.values()))
    assert session.exit_reason == "dwell"
    assert session.status is SessionStatus.COMPLETED
    assert by_type(bus, VEHICLE_EXITED)[0]["exit_reason"] == "dwell"


def test_a_dwell_exit_dated_before_its_own_entry_is_an_anomaly_not_a_negative_visit():
    """Real footage, 2026-09-30: on `gate_traffic.mp4` a dwell exit arrived stamped
    at the vehicle's last sighting in the zone, which was four frames *before* the
    entry the same track id had just opened. SQLite's ordering CHECK refused the
    row - correctly - and this is what the state machine says instead: keep the
    entry, claim no exit, report the contradiction."""
    sessions, bus = service()
    sessions.crossing(entry(at=T0))
    sessions.plate(read(), CAM)
    events = sessions.crossing(exit_(at=T0 - 0.133, dwell=True))

    session = next(iter(sessions.sessions.values()))
    assert session.status is SessionStatus.ANOMALY
    assert session.anomaly_reason == UNORDERED_EXIT
    assert (session.exit_at, session.duration_s) == (None, None)
    assert [e.type for e in events] == [VIOLATION]
    assert by_type(bus, VEHICLE_EXITED) == []
    violation = by_type(bus, VIOLATION)[0]
    assert violation["kind"] == UNORDERED_EXIT
    # The claimed time is evidence, so it is in the event even though the row
    # refuses to store it as this visit's exit.
    assert violation["claimed_exit_ts"] == pytest.approx(T0 - 0.133, abs=1e-3)
    assert violation["claimed_exit_reason"] == "dwell"


# -- the two anomalies the doc names ----------------------------------------
def test_a_second_active_session_for_one_plate_is_an_anomaly():
    sessions, bus = service()
    sessions.crossing(entry(track_id=1))
    sessions.plate(read(track_id=1), CAM)
    sessions.crossing(entry(track_id=2))
    sessions.plate(read(track_id=2), CAM)

    both = list(sessions.sessions.values())
    assert [s.status for s in both] == [SessionStatus.ACTIVE, SessionStatus.ANOMALY]
    assert both[1].anomaly_reason == DUPLICATE_PLATE
    violations = by_type(bus, VIOLATION)
    assert len(violations) == 1
    assert violations[0]["kind"] == DUPLICATE_PLATE
    assert violations[0]["plate"] == GOOD_PLATE


def test_an_anomalous_session_still_closes_and_keeps_its_duration():
    sessions, bus = service()
    sessions.crossing(entry(track_id=1, at=T0))
    sessions.plate(read(track_id=1), CAM)
    sessions.crossing(entry(track_id=2, at=T0 + 5.0))
    sessions.plate(read(track_id=2), CAM)
    sessions.crossing(exit_(track_id=2, at=T0 + 45.0))

    second = list(sessions.sessions.values())[1]
    assert second.status is SessionStatus.ANOMALY  # the flag survives closing
    assert second.duration_s == pytest.approx(40.0)
    assert second not in sessions.open_sessions()
    assert len(by_type(bus, VEHICLE_EXITED)) == 1


def test_an_exit_with_no_entry_is_a_violation_not_a_fake_duration():
    sessions, bus = service()
    events = sessions.crossing(exit_(at=T0))
    assert [e.type for e in events] == [VIOLATION]
    assert by_type(bus, VEHICLE_EXITED) == []
    session = next(iter(sessions.sessions.values()))
    assert session.status is SessionStatus.ANOMALY
    assert session.anomaly_reason == NO_ENTRY
    assert session.entry_at is None
    assert session.duration_s is None


# -- plate confidence and review --------------------------------------------
def test_a_weak_plate_stays_a_session_but_not_a_silent_accept():
    sessions, bus = service()
    sessions.crossing(entry())
    session = sessions.plate(read(ocr=0.62), CAM)
    assert session.review_reason and "0.62" in session.review_reason
    sessions.crossing(exit_())
    assert session.status is SessionStatus.NEEDS_REVIEW
    assert session.duration_s == pytest.approx(80.0)
    assert by_type(bus, VEHICLE_EXITED)[0]["status"] == "NEEDS_REVIEW"


def test_the_threshold_comes_from_the_gate_config():
    sessions, _ = service(min_ocr_confidence=0.5)
    sessions.crossing(entry())
    session = sessions.plate(read(ocr=0.62), CAM)
    assert session.review_reason is None
    sessions.crossing(exit_())
    assert session.status is SessionStatus.COMPLETED


def test_a_session_that_never_got_a_plate_is_flagged_at_exit():
    sessions, _ = service()
    sessions.crossing(entry())
    sessions.crossing(exit_())
    session = next(iter(sessions.sessions.values()))
    assert session.review_reason == "no plate read"
    assert session.status is SessionStatus.NEEDS_REVIEW


def test_a_changed_plate_is_bound_but_flagged():
    """A later stable read for the same track either corrects the vote or means
    the id moved to another vehicle - both need a human, not a silent swap."""
    sessions, _ = service()
    sessions.crossing(entry())
    sessions.plate(read(plate=GOOD_PLATE), CAM)
    sessions.plate(read(plate="MH14AB1235"), CAM)
    session = next(iter(sessions.sessions.values()))
    assert session.plate == "MH14AB1235"
    assert session.review_reason == "plate changed"


# -- stale plates ------------------------------------------------------------
def test_a_pending_plate_expires_rather_than_reaching_the_next_car():
    sessions, _ = service(track_timeout_s=5.0)
    sessions.plate(read(track_id=3, at=T0), CAM)
    sessions.prune(T0 + 6.0)
    sessions.crossing(entry(track_id=3, at=T0 + 10.0))
    session = next(iter(sessions.sessions.values()))
    assert session.plate is None


def test_a_pending_plate_within_the_timeout_still_binds():
    sessions, _ = service(track_timeout_s=5.0)
    sessions.plate(read(track_id=3, at=T0), CAM)
    sessions.prune(T0 + 2.0)
    sessions.crossing(entry(track_id=3, at=T0 + 3.0))
    assert next(iter(sessions.sessions.values())).plate == GOOD_PLATE


# -- queries and reset -------------------------------------------------------
def test_slot_binding_is_explicit_and_optional():
    sessions, _ = service()
    sessions.crossing(entry())
    session = next(iter(sessions.sessions.values()))
    assert session.slot_id is None  # nothing has proved which slot it took
    assert sessions.bind_slot(session.session_id, "A3") is session
    assert session.slot_id == "A3"
    assert sessions.bind_slot("does-not-exist", "A3") is None


def test_active_for_plate_ignores_closed_sessions():
    sessions, _ = service()
    sessions.crossing(entry(at=T0))
    sessions.plate(read(), CAM)
    assert sessions.active_for_plate(GOOD_PLATE) is not None
    sessions.crossing(exit_(at=T0 + 10.0))
    assert sessions.active_for_plate(GOOD_PLATE) is None


def test_reset_forgets_everything():
    sessions, _ = service()
    sessions.crossing(entry())
    sessions.plate(read(track_id=99), CAM)
    sessions.reset()
    assert sessions.sessions == {}
    assert sessions.open_sessions() == []
    assert sessions.crossing(entry()) != []


def test_a_service_without_a_bus_still_tracks_sessions():
    sessions = SessionService(GateConfig())
    assert sessions.crossing(entry()) == []
    assert len(sessions.sessions) == 1


# -- the seam the doc describes ---------------------------------------------
def test_gate_and_session_modules_never_touch_the_database_or_api():
    """`services/session_service.py` is the seam: it may know about the AI side,
    but nothing above Phase 7 may pull a database or a web framework in."""
    root = Path(__file__).resolve().parents[2] / "src" / "smart_parking"
    offenders = []
    for path in [
        root / "services" / "session_service.py",
        root / "parking" / "gate_logic.py",
        root / "pipeline" / "gate_pipeline.py",
    ]:
        text = path.read_text(encoding="utf-8")
        if search(r"^\s*(import|from)\s+(smart_parking\.(db|api)|sqlalchemy|fastapi)", text, MULTILINE):
            offenders.append(path.name)
    assert offenders == []
