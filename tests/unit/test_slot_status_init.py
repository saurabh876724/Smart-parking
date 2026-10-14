"""A bay the camera can see must not sit at UNKNOWN for the rest of the run.

The bug these tests pin down: `SlotRepository.sync` creates a bay at UNKNOWN, and
the occupancy engine only ever published a *transition*. A bay that stayed empty
the whole time therefore produced no event at all, so its row kept the
placeholder it was born with and a dashboard showed zero available spaces next to
a camera that was very much online.

The fix is an announcement of each bay's first committed reading, under its own
event type. Two promises had to survive it, and both are tested here:

* UNKNOWN still means "nobody looked at this bay" - it was not simply renamed to
  FREE at seed time, which would report a dead camera as spare capacity; and
* the existing transition counts are untouched, because the announcement is not
  a `slot_occupied` or a `slot_freed` wearing a different hat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smart_parking.core.config import (
    AppConfig,
    CameraConfig,
    OccupancyConfig,
    load_slots,
)
from smart_parking.core.events import RecordingEventBus
from smart_parking.core.schemas import (
    SLOT_FREED,
    SLOT_OCCUPIED,
    SLOT_SNAPSHOT,
    CameraRole,
    Point,
    SlotStatus,
    Track,
)
from smart_parking.db.base import to_utc
from smart_parking.db.repositories import Repositories
from smart_parking.parking.geometry import slots_from_config
from smart_parking.parking.occupancy_engine import OccupancyEngine
from smart_parking.services.persistence import (
    apply_events,
    plate_normaliser_for,
    sync_configuration,
)

ROOT = Path(__file__).resolve().parents[2]
LOT = "lot_cam1"
FRAME = (1000, 500)
T0 = 1000.0

# The shipped calibration file, so the seeded rows and the engine agree on the
# bay codes - which is the whole subject of this file.
LOT_CAMERA = CameraConfig(
    id=LOT,
    name="Lot camera 1",
    role=CameraRole.LOT,
    source="0",
    slots_file="configs/slots/lot_a_cam1.json",
)
CFG = AppConfig(cameras=[LOT_CAMERA], project_root=ROOT)
NORMALISE = plate_normaliser_for(CFG)
CALIBRATED = slots_from_config(load_slots(LOT_CAMERA.slots_file, ROOT), LOT)
BAYS = sorted(CALIBRATED)


def rect(x1, y1, x2, y2):
    return (Point(x1, y1), Point(x2, y1), Point(x2, y2), Point(x1, y2))


# Two 200x100 bays, the same shape the occupancy engine tests use, so a car can
# be parked in one without depending on the real calibration geometry.
SYNTHETIC = {"A1": rect(0.10, 0.60, 0.30, 0.80), "A2": rect(0.30, 0.60, 0.50, 0.80)}


def track(track_id, x1, y1, x2, y2) -> Track:
    return Track(
        bbox=(x1, y1, x2, y2),
        confidence=0.9,
        class_id=2,
        class_name="car",
        track_id=track_id,
        footprint_ratio=0.4,
    )


def parked(track_id=1) -> Track:
    """A footprint sitting entirely inside synthetic bay A1."""
    return track(track_id, 120, 250, 280, 390)


def make_engine(slots=None, **over):
    settings = {
        "occupy_score": 0.45,
        "any_slot_score": 0.20,
        "straddle_score": 0.20,
        "occupied_after_s": 0.5,
        "free_after_s": 8.0,
    }
    settings.update(over)
    bus = RecordingEventBus()
    return OccupancyEngine(LOT, slots or SYNTHETIC, OccupancyConfig(**settings), bus), bus


@pytest.fixture()
def seeded(memory_database):
    """The real lot, with its bays created exactly the way a pipeline run creates
    them: one row per calibrated bay, none of them observed yet."""
    sync_configuration(memory_database, CFG, normalise=NORMALISE)
    return memory_database


def statuses(database, camera_id=LOT) -> dict[str, SlotStatus]:
    with database.scope() as session:
        rows = Repositories(session, normalise=NORMALISE).slots.for_camera(camera_id)
    return {row.slot_code: row.current_status for row in rows}


# -- the announcement itself -------------------------------------------------
def test_the_first_frame_announces_every_bay_once():
    engine, bus = make_engine()
    engine.update([], T0, FRAME)
    announced = bus.by_type(SLOT_SNAPSHOT)
    assert sorted(e.payload["slot_id"] for e in announced) == ["A1", "A2"]
    assert [e.payload["status"] for e in announced] == ["FREE", "FREE"]

    engine.update([], T0 + 1.0, FRAME)
    assert len(bus.by_type(SLOT_SNAPSHOT)) == 2, "the announcement is once per bay, not per frame"


def test_the_announcement_is_not_counted_as_a_transition():
    """One stay is still one `slot_occupied` and one `slot_freed`.

    The occupancy tests assert those counts exactly; this is the same promise
    seen from the other side, so neither layer can quietly start double-booking
    an interval.
    """
    engine, bus = make_engine()
    for i in range(10):
        engine.update([parked()], T0 + i * 0.2, FRAME)
    for i in range(30):
        engine.update([], T0 + 2.0 + i * 0.5, FRAME)

    assert engine.fsms["A1"].status is SlotStatus.FREE
    assert len(bus.by_type(SLOT_OCCUPIED)) == 1
    assert len(bus.by_type(SLOT_FREED)) == 1
    assert len(bus.by_type(SLOT_SNAPSHOT)) == 2


def test_a_bay_mid_dwell_is_announced_as_its_committed_state():
    """PENDING_OCCUPIED is a timer, not a fact about the lot.

    The column only ever holds a committed status, and the API documents that a
    row cannot be PENDING_OCCUPIED - so the announcement must not be the one
    thing that breaks that.
    """
    engine, bus = make_engine()
    engine.update([parked()], T0, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.PENDING_OCCUPIED
    assert engine.fsms["A1"].committed is SlotStatus.FREE

    announced = {e.payload["slot_id"]: e.payload["status"] for e in bus.by_type(SLOT_SNAPSHOT)}
    assert announced == {"A1": "FREE", "A2": "FREE"}
    assert not any(
        str(e.payload["status"]).startswith("PENDING_") for e in bus.by_type(SLOT_SNAPSHOT)
    )


def test_a_bay_that_commits_on_the_first_frame_is_not_announced_too():
    """With no dwell time the first frame is already a transition, so the
    announcement stands down for that bay rather than reporting it twice."""
    engine, bus = make_engine(occupied_after_s=0.0)
    engine.update([parked()], T0, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED
    assert [e.payload["slot_id"] for e in bus.by_type(SLOT_SNAPSHOT)] == ["A2"]
    assert len(bus.by_type(SLOT_OCCUPIED)) == 1


def test_an_engine_with_no_bus_still_runs():
    """The announcement must not require a consumer; the replay tools build an
    engine without one."""
    engine = OccupancyEngine(LOT, SYNTHETIC, OccupancyConfig(occupy_score=0.45))
    states = engine.update([], T0, FRAME)
    assert {s.slot_id: s.status for s in states}["A1"] is SlotStatus.FREE


# -- what reaches the database ------------------------------------------------
def test_an_empty_lot_reaches_the_database_as_free(seeded):
    """The reported bug, end to end: five bays, a live camera, nothing parked.

    Before the announcement these rows all kept UNKNOWN and the dashboard could
    only ever report zero available spaces.
    """
    engine, bus = make_engine(CALIBRATED)
    engine.update([], T0, FRAME)
    assert apply_events(seeded, bus.recorded, normalise=NORMALISE) == len(bus.recorded)

    assert statuses(seeded) == {code: SlotStatus.FREE for code in BAYS}


def test_a_bay_nobody_ever_looked_at_stays_unknown(seeded):
    """The other half of the fix: UNKNOWN was not just renamed at seed time.

    A row created but never evaluated still says UNKNOWN, which is the only way
    an operator can tell "empty" from "the camera is down".
    """
    assert statuses(seeded) == {code: SlotStatus.UNKNOWN for code in BAYS}


def test_the_reading_replaces_unknown_once_the_camera_has_looked(seeded):
    engine, bus = make_engine(CALIBRATED)
    engine.update([], T0, FRAME)
    apply_events(seeded, bus.recorded, normalise=NORMALISE)

    with seeded.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        row = repos.slots.find(LOT, "A1")
        assert row.current_status is SlotStatus.FREE
        # The placeholder had no time attached; a real reading does, so the two
        # are distinguishable even after the status happens to agree.
        assert row.status_since is not None


def test_the_reading_closes_an_interval_an_earlier_run_left_open(seeded):
    """A restart must not strand a bay as occupied forever.

    The previous run recorded A2 as occupied and never saw it empty. The new run
    looks at it, finds nothing, and its reading has to close that interval -
    which is why the announcement reuses the transition writes instead of
    inventing a third kind of one.
    """
    with seeded.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        bay = repos.slots.find(LOT, "A2")
        repos.slots.set_status(bay, SlotStatus.OCCUPIED, to_utc(T0 - 60.0), track_id=7)
        repos.occupancy.open_interval(bay, to_utc(T0 - 60.0), track_id=7)

    assert statuses(seeded)["A2"] is SlotStatus.OCCUPIED

    engine, bus = make_engine(CALIBRATED)
    engine.update([], T0, FRAME)
    apply_events(seeded, bus.recorded, normalise=NORMALISE)

    assert statuses(seeded)["A2"] is SlotStatus.FREE
    with seeded.scope() as session:
        repos = Repositories(session, normalise=NORMALISE)
        bay = repos.slots.find(LOT, "A2")
        assert repos.occupancy.open_for(bay.id) is None
        assert len(repos.occupancy.history(bay.id)) == 1


def test_the_counts_a_dashboard_reads_add_up_to_the_bays(seeded):
    """occupied + available + unknown is every bay, before and after a reading.

    The cards on the dashboard are these three numbers and the total; if a bay
    could fall outside all of them the layout and the counters would disagree,
    which is what this file started out reporting.
    """
    with seeded.scope() as session:
        before = Repositories(session, normalise=NORMALISE).slots.status_counts(LOT)
    assert sum(before.values()) == len(BAYS)
    assert before.get(SlotStatus.UNKNOWN, 0) == len(BAYS)

    engine, bus = make_engine(CALIBRATED)
    engine.update([], T0, FRAME)
    apply_events(seeded, bus.recorded, normalise=NORMALISE)

    with seeded.scope() as session:
        after = Repositories(session, normalise=NORMALISE).slots.status_counts(LOT)
    assert sum(after.values()) == len(BAYS)
    assert after.get(SlotStatus.UNKNOWN, 0) == 0
    assert after[SlotStatus.FREE] == len(BAYS)
