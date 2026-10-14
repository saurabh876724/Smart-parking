"""Occupancy engine tests: assignment rules and emitted events."""

from __future__ import annotations

from smart_parking.core.config import OccupancyConfig, VelocityGatingConfig
from smart_parking.core.events import RecordingEventBus
from smart_parking.core.schemas import SLOT_FREED, SLOT_OCCUPIED, Point, SlotStatus, Track
from smart_parking.parking.occupancy_engine import OccupancyEngine
from smart_parking.tracking.velocity import VelocityGate

T0 = 1000.0
FRAME = (1000, 500)  # width, height


def rect(x1, y1, x2, y2):
    return (Point(x1, y1), Point(x2, y1), Point(x2, y2), Point(x1, y2))


# Two 200x100 bays side by side in a 1000x500 frame.
SLOTS = {
    "A1": rect(100 / 1000, 300 / 500, 300 / 1000, 400 / 500),
    "A2": rect(300 / 1000, 300 / 500, 500 / 1000, 400 / 500),
}


def track(track_id, x1, y1, x2, y2) -> Track:
    return Track(
        bbox=(x1, y1, x2, y2),
        confidence=0.9,
        class_id=2,
        class_name="car",
        track_id=track_id,
        footprint_ratio=0.4,
    )


def make(**over):
    cfg = OccupancyConfig(occupy_score=0.45, any_slot_score=0.20, straddle_score=0.20,
                          occupied_after_s=0.5, free_after_s=8.0)
    for key, value in over.items():
        setattr(cfg, key, value)
    bus = RecordingEventBus()
    return OccupancyEngine("cam1", SLOTS, cfg, bus), bus


def parked(track_id=1):
    """A car whose footprint sits entirely inside A1."""
    return track(track_id, 120, 250, 280, 390)


def settle(engine, t=T0, seconds=1.0, step=0.2, tr=None):
    tr = tr if tr is not None else parked()
    for i in range(int(seconds / step)):
        engine.update([tr], t + i * step, FRAME)
    return engine.update([tr], t + seconds, FRAME)


def test_car_parks_then_slot_is_occupied():
    engine, bus = make()
    settle(engine)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED
    assert engine.fsms["A2"].status is SlotStatus.FREE
    assert [e.type for e in bus.by_type(SLOT_OCCUPIED)] == [SLOT_OCCUPIED]


def test_summary_counts_pending_free_as_occupied():
    engine, _ = make()
    settle(engine)
    assert engine.summary() == {"total": 2, "occupied": 1, "available": 1, "unknown": 0}
    # Car leaves: the long free timer means it still counts as taken.
    for i in range(10):
        engine.update([], T0 + 1.0 + i * 0.5, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.PENDING_FREE
    assert engine.summary()["occupied"] == 1


def test_slot_frees_after_the_long_timer():
    engine, bus = make()
    settle(engine)
    for i in range(30):
        engine.update([], T0 + 1.0 + i * 0.5, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.FREE
    assert [e.type for e in bus.by_type(SLOT_FREED)] == [SLOT_FREED]


def test_one_vehicle_claims_at_most_one_slot():
    """A wide footprint spanning both bays must not book both."""
    engine, _ = make()
    wide = track(1, 150, 250, 450, 390)
    settle(engine, tr=wide)
    occupied = [s for s, fsm in engine.fsms.items() if fsm.status is SlotStatus.OCCUPIED]
    assert len(occupied) == 1


def test_two_vehicles_take_two_slots():
    engine, _ = make()
    a = track(1, 120, 250, 280, 390)   # inside A1
    b = track(2, 320, 250, 480, 390)   # inside A2
    for i in range(8):
        engine.update([a, b], T0 + i * 0.2, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED
    assert engine.fsms["A2"].status is SlotStatus.OCCUPIED


def test_straddling_vehicle_raises_violation():
    engine, bus = make()
    straddler = track(1, 220, 250, 380, 390)  # split evenly across the line
    settle(engine, tr=straddler)
    violations = bus.by_type("violation")
    assert violations, "expected a DOUBLE_PARK violation"
    assert len(violations[0].payload["slots"]) == 2


def test_car_in_aisle_claims_nothing():
    engine, _ = make()
    aisle = track(1, 100, 100, 260, 240)  # footprint above both bays
    settle(engine, tr=aisle)
    assert all(f.status is SlotStatus.FREE for f in engine.fsms.values())


def test_frame_skipping_still_occupies():
    """Regression: skipped inference frames must hold the last evidence.

    Feeding an empty track list on skipped frames used to reset every dwell
    timer, so no slot could ever reach OCCUPIED at --stride 2 or higher.
    """
    engine, _ = make()
    car = parked()
    for i in range(12):
        if i % 3 == 0:
            engine.update([car], T0 + i * 0.2, FRAME)
        else:
            engine.tick(T0 + i * 0.2)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED


def test_tick_after_car_leaves_still_frees_eventually():
    engine, _ = make()
    settle(engine)
    engine.update([], T0 + 1.0, FRAME)
    for i in range(60):
        engine.tick(T0 + 1.2 + i * 0.2)
    assert engine.fsms["A1"].status is SlotStatus.FREE


def test_mark_all_unknown_on_camera_loss():
    engine, _ = make()
    settle(engine)
    engine.mark_all(SlotStatus.UNKNOWN, T0 + 2.0)
    assert engine.summary()["unknown"] == 2
    assert engine.summary()["available"] == 0


def test_same_calibration_works_at_two_resolutions():
    """Phase 2 acceptance: the config loads correctly at a different resolution.

    The car below is parked in A1 in 1000x500 space; the same geometry scaled
    to 1920x1080 must still be recognised as parked in A1.
    """
    engine, _ = make()
    settle(engine)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED

    bigger = OccupancyEngine("cam1", SLOTS, engine.cfg)
    scaled = track(9, 120 * 1.92, 250 * 2.16, 280 * 1.92, 390 * 2.16)
    for i in range(8):
        bigger.update([scaled], T0 + i * 0.2, (1920, 1080))
    assert bigger.fsms["A1"].status is SlotStatus.OCCUPIED
    assert bigger.fsms["A1"].track_id == 9


# -- Phase 4: velocity gating ----------------------------------------------
def build_engine(gate):
    cfg = OccupancyConfig(occupy_score=0.45, any_slot_score=0.20, straddle_score=0.20,
                          occupied_after_s=0.5, free_after_s=8.0)
    bus = RecordingEventBus()
    return OccupancyEngine("cam1", SLOTS, cfg, bus, gate=gate), bus


def gated(**over):
    """An engine with the Phase 4 filter wired in the way the pipeline does it."""
    settings = {"smoothing_window": 3, "max_parking_velocity": 30.0}
    settings.update(over)
    return build_engine(VelocityGate(VelocityGatingConfig(**settings)))


def ungated():
    return build_engine(None)


def drive(engine, centres, y1=250, y2=390, step=0.1, t0=T0, track_id=1):
    """Move a 160px-wide car across the frame, one centre x per frame."""
    for i, cx in enumerate(centres):
        engine.update([track(track_id, cx - 80, y1, cx + 80, y2)], t0 + i * step, FRAME)
    return t0 + len(centres) * step


AISLE_CROSSING = [100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300]  # 200 px/s


def test_fast_vehicle_crossing_a_bay_never_claims_it():
    """Test A: the aisle car overlaps A1 for longer than the dwell time."""
    engine, bus = gated()
    drive(engine, AISLE_CROSSING)
    assert bus.by_type(SLOT_OCCUPIED) == []
    assert engine.fsms["A1"].status is SlotStatus.FREE


def test_the_same_crossing_without_gating_would_claim_the_bay():
    """Proves the gate, not the geometry, is what rejects the passing car.

    Containment alone cannot tell this apart from parking: the footprint sits
    fully inside A1 for 0.5s+, past the dwell threshold.
    """
    engine, bus = ungated()
    drive(engine, AISLE_CROSSING)
    assert len(bus.by_type(SLOT_OCCUPIED)) == 1


def test_vehicle_that_slows_and_stops_in_the_bay_occupies_it():
    """Test B: approach at speed, brake inside the bay, stay put."""
    engine, bus = gated()
    t = drive(engine, [-100, -20, 60, 140], step=0.1)          # 800 px/s approach
    assert engine.fsms["A1"].status is SlotStatus.FREE
    for i in range(12):                                          # parked in A1
        engine.update([track(1, 120, 250, 280, 390)], t + i * 0.1, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED
    assert len(bus.by_type(SLOT_OCCUPIED)) == 1


def test_gating_only_affects_eligibility_not_the_free_timer():
    """A parked car that drives off still goes through the long free timer."""
    engine, _ = gated()
    for i in range(10):
        engine.update([track(1, 120, 250, 280, 390)], T0 + i * 0.1, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED

    t = drive(engine, [200, 300, 400, 500], step=0.1, t0=T0 + 1.0)  # leaves at speed
    assert engine.fsms["A1"].status is SlotStatus.PENDING_FREE
    engine.update([], t + 7.0, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.PENDING_FREE
    engine.update([], t + 8.2, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.FREE


def test_gating_threshold_from_config_changes_the_outcome():
    """Test D at engine level: the same motion, two configured thresholds."""
    strict, strict_bus = gated()
    drive(strict, AISLE_CROSSING)
    assert strict_bus.by_type(SLOT_OCCUPIED) == []
    assert strict.fsms["A1"].status is SlotStatus.FREE

    lenient, lenient_bus = gated(max_parking_velocity=5000.0)
    drive(lenient, AISLE_CROSSING)
    assert len(lenient_bus.by_type(SLOT_OCCUPIED)) == 1


def test_no_track_mode_is_ungated_and_still_occupies():
    """Test E: synthetic per-frame ids carry no motion, so nothing is gated."""
    cfg = OccupancyConfig(occupy_score=0.45, any_slot_score=0.20, straddle_score=0.20,
                          occupied_after_s=0.5, free_after_s=8.0)
    engine = OccupancyEngine("cam1", SLOTS, cfg, RecordingEventBus(),
                             identity_stable=False, gate=None)
    for i in range(10):
        engine.update([track(100 + i, 120, 250, 280, 390)], T0 + i * 0.1, FRAME)
    assert engine.fsms["A1"].status is SlotStatus.OCCUPIED


def test_disabled_gate_reproduces_phase3_behaviour():
    engine, bus = gated(enabled=False)
    drive(engine, AISLE_CROSSING)
    assert len(bus.by_type(SLOT_OCCUPIED)) == 1
