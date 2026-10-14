"""Gate line crossing (Phase 6, test group: line + zone rules).

Everything here is hand-written centroid sequences with known answers, which is
how the doc says to test this stage: "scripted clips with known times; test
jitter and re-entry". No video, no OCR, no sessions.

Two coordinate spaces appear on purpose: tracks are in pixels because that is
what the tracker emits, and gate geometry is normalised 0-1 because that is what
a gate file stores. ``car()`` converts one into the other.
"""

from __future__ import annotations

import pytest

from smart_parking.core.config import GateConfig
from smart_parking.core.schemas import Track
from smart_parking.parking.gate_logic import EXIT, ENTRY, GateLogic, segments_cross, side

T0 = 100.0
FRAME = (480, 360)
# Drawn left to right at y=0.8, so traffic flowing down the frame crosses
# positive (see the module docstring's convention).
ENTRY_LINE = {"name": "entry", "points": [[0.1, 0.8], [0.9, 0.8]], "direction": "positive", "role": "ENTRY"}
EXIT_LINE = {"name": "exit", "points": [[0.1, 0.4], [0.9, 0.4]], "direction": "negative", "role": "EXIT"}
ZONE = [[0.05, 0.1], [0.95, 0.1], [0.95, 0.95], [0.05, 0.95]]


def car(
    fx: float, fy: float, track_id: int = 1, frame: tuple[int, int] = FRAME,
    width: float = 120.0, height: float = 60.0,
) -> Track:
    """A vehicle whose centroid sits at normalised (fx, fy) in ``frame``."""
    x, y = fx * frame[0], fy * frame[1]
    return Track(
        bbox=(x - width / 2, y - height / 2, x + width / 2, y + height / 2),
        confidence=0.9,
        class_id=2,
        class_name="car",
        track_id=track_id,
    )


def gate(**overrides) -> GateLogic:
    cfg = GateConfig(lines=[ENTRY_LINE], zone=[], **overrides)
    return GateLogic(cfg, camera_id="gate_cam1")


def drive(gate_logic: GateLogic, positions, start_t: float = T0, step_s: float = 0.1,
          frame: tuple[int, int] = FRAME):
    """Feed a centroid path, return every event produced."""
    out = []
    for i, (fx, fy) in enumerate(positions):
        now = start_t + i * step_s
        out.extend(gate_logic.update([car(fx, fy, frame=frame)], now, frame))
        out.extend(gate_logic.prune(now))
    return out


# -- the direction convention itself ----------------------------------------
def test_side_sign_matches_the_documented_convention():
    line_a, line_b = (0.1, 0.8), (0.9, 0.8)
    assert side(line_a, line_b, (0.5, 0.75)) < 0  # above the line
    assert side(line_a, line_b, (0.5, 0.85)) > 0  # below it


def test_downward_traffic_across_a_left_to_right_line_is_positive():
    crossings = drive(gate(), [(0.5, 0.75), (0.5, 0.79), (0.5, 0.82), (0.5, 0.86)])
    assert [c.role for c in crossings] == [ENTRY]
    assert crossings[0].direction == "positive"


def test_segments_cross_only_on_a_proper_crossing():
    a, b = (0.1, 0.8), (0.9, 0.8)
    assert segments_cross((0.5, 0.78), (0.5, 0.82), a, b)
    assert not segments_cross((0.5, 0.78), (0.5, 0.79), a, b)  # never reaches the line
    assert not segments_cross((0.02, 0.78), (0.02, 0.82), a, b)  # crosses the line's extent, not the segment
    assert not segments_cross((0.5, 0.8), (0.5, 0.82), a, b)  # starts on the line


# -- one event per track per role --------------------------------------------
def test_each_track_fires_an_entry_at_most_once():
    crossings = drive(gate(), [(0.5, 0.79), (0.5, 0.82), (0.5, 0.79), (0.5, 0.82)])
    assert len([c for c in crossings if c.role == ENTRY]) == 1


def test_jitter_around_the_line_does_not_double_count():
    """The failure the doc warns about: a car edging forward at the barrier."""
    positions = [(0.5, 0.83 if i % 2 == 0 else 0.79) for i in range(12)]
    crossings = drive(gate(), positions)
    assert len(crossings) == 1


def test_reentry_after_crossing_produces_no_second_entry():
    logic = gate()
    crossings = drive(logic, [(0.5, 0.7), (0.5, 0.9), (0.5, 0.7), (0.5, 0.9)])
    assert [c.role for c in crossings] == [ENTRY]
    assert logic.reversals[(1, "entry")] == 1  # the trip back through the line
    assert logic.duplicates[(1, "entry")] == 1  # the re-crossing that stayed quiet


def test_a_vehicle_travelling_the_wrong_way_never_fires():
    crossings = drive(gate(), [(0.5, 0.9), (0.5, 0.85), (0.5, 0.7), (0.5, 0.6)])
    assert crossings == []


def test_disabled_gate_produces_nothing():
    logic = GateLogic(GateConfig(lines=[ENTRY_LINE], enabled=False), "gate_cam1")
    assert drive(logic, [(0.5, 0.7), (0.5, 0.9)]) == []


def test_reset_forgets_which_tracks_already_reported():
    logic = gate()
    assert len(drive(logic, [(0.5, 0.7), (0.5, 0.9)])) == 1
    logic.reset()
    assert len(drive(logic, [(0.5, 0.7), (0.5, 0.9)])) == 1


# -- timing and geometry details ---------------------------------------------
def test_event_carries_video_time_not_wall_clock():
    crossings = drive(gate(), [(0.5, 0.7), (0.5, 0.9)], start_t=T0, step_s=0.5)
    assert crossings[0].timestamp == pytest.approx(T0 + 0.5)


def test_a_fast_vehicle_skipping_over_the_line_between_frames_still_counts():
    """Centroid jumps from well above to well below with nothing in between."""
    crossings = drive(gate(), [(0.5, 0.5), (0.5, 0.95)])
    assert [c.role for c in crossings] == [ENTRY]


def test_two_tracks_are_counted_independently():
    logic = gate()
    logic.update([car(0.5, 0.7, track_id=1), car(0.3, 0.7, track_id=2)], T0, FRAME)
    events = logic.update(
        [car(0.5, 0.9, track_id=1), car(0.3, 0.9, track_id=2)], T0 + 0.1, FRAME
    )
    assert sorted(e.track_id for e in events) == [1, 2]


def test_entry_and_exit_lines_are_their_own_roles():
    cfg = GateConfig(lines=[ENTRY_LINE, EXIT_LINE], zone=[])
    logic = GateLogic(cfg, camera_id="gate_cam1")
    # Out of the lot: up the frame, crossing y=0.4 negatively. The samples stay
    # off the line itself - a centroid exactly on the barrier is not a crossing.
    crossings = drive(logic, [(0.5, 0.55), (0.5, 0.5), (0.5, 0.45), (0.5, 0.35)])
    assert [(c.role, c.line) for c in crossings] == [(EXIT, "exit")]


def test_the_event_reports_the_pixel_centroid_for_overlay_drawing():
    crossings = drive(gate(), [(0.5, 0.7), (0.5, 0.9)])
    assert crossings[0].centroid == pytest.approx((0.5 * FRAME[0], 0.9 * FRAME[1]))


def test_the_same_drawing_works_at_another_resolution():
    """Phase 2's acceptance test, applied to gates: a resized frame or a camera
    that changed resolution must not need the lines redrawn."""
    normal = drive(gate(), [(0.5, 0.7), (0.5, 0.9)])
    upscaled = drive(gate(), [(0.5, 0.7), (0.5, 0.9)], frame=(960, 720))
    assert [c.role for c in normal] == [ENTRY]
    assert [c.role for c in upscaled] == [ENTRY]


# -- the zone ----------------------------------------------------------------
def test_zone_excludes_crossings_outside_the_gate_area():
    logic = GateLogic(GateConfig(lines=[ENTRY_LINE], zone=[[0.1, 0.85], [0.9, 0.85], [0.9, 0.95], [0.1, 0.95]]))
    # The step happens above the zone, so the line crossing is not the barrier.
    assert drive(logic, [(0.5, 0.7), (0.5, 0.9)]) == []

    inside = GateLogic(GateConfig(lines=[ENTRY_LINE], zone=ZONE))
    assert [c.role for c in drive(inside, [(0.5, 0.7), (0.5, 0.9)])] == [ENTRY]


def test_dwell_turns_silence_in_the_zone_into_an_exit():
    cfg = GateConfig(lines=[ENTRY_LINE], zone=ZONE, dwell_exit_s=12.0, track_timeout_s=30.0)
    logic = GateLogic(cfg, camera_id="gate_cam1")
    # The first sample only establishes where the vehicle came from; a crossing
    # needs a step, so nothing fires until the second one.
    assert logic.update([car(0.5, 0.7)], T0, FRAME) == []
    crossings = logic.update([car(0.5, 0.9)], T0 + 0.1, FRAME)
    assert [c.role for c in crossings] == [ENTRY]
    assert logic.prune(T0 + 6.0) == []  # not gone long enough yet
    exits = logic.prune(T0 + 12.5)
    assert [(e.role, e.direction) for e in exits] == [(EXIT, "dwell")]
    # Timestamped when it was last seen, not when the timer ran out.
    assert exits[0].timestamp == pytest.approx(T0 + 0.1)


def test_a_track_that_never_entered_the_zone_has_no_dwell_exit():
    logic = GateLogic(GateConfig(lines=[ENTRY_LINE], zone=ZONE, dwell_exit_s=1.0), "gate_cam1")
    # Inside the frame but above the zone's top edge at y=0.1.
    logic.update([car(0.5, 0.03)], T0, FRAME)
    assert logic.prune(T0 + 5.0) == []


def test_a_vehicle_still_visible_past_the_zone_edge_is_not_an_exit():
    """The dwell timer measures "not seen", not "left the zone".

    A car that has driven a few metres past the gate area is still in the
    picture; treating the zone edge as the clock start would close its session
    while the operator watches it sitting there.
    """
    zone = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.95], [0.1, 0.95]]
    cfg = GateConfig(lines=[ENTRY_LINE], zone=zone, dwell_exit_s=2.0, track_timeout_s=30.0)
    logic = GateLogic(cfg, "gate_cam1")
    events = []
    # Down through the entry line inside the zone, then sideways out of it while
    # remaining fully visible.
    path = [(0.3, 0.7), (0.3, 0.85), (0.45, 0.85), (0.55, 0.85), (0.7, 0.85)]
    for i, (fx, fy) in enumerate(path):
        now = T0 + i * 0.5
        events += logic.update([car(fx, fy)], now, FRAME)
        events += logic.prune(now)
    assert [e.role for e in events] == [ENTRY]

    # Still visible outside the zone for 20 s of frames - long past the timer.
    for now in (T0 + 5.0, T0 + 10.0, T0 + 15.0, T0 + 20.0):
        assert logic.update([car(0.7, 0.85)], now, FRAME) == []
        assert logic.prune(now) == []

    # Only when it stops being tracked does the clock run out, and the exit is
    # timestamped at its last in-zone sighting (above), not at the timer.
    assert logic.prune(T0 + 21.9) == []
    exits = logic.prune(T0 + 22.1)
    assert [(e.role, e.direction) for e in exits] == [(EXIT, "dwell")]
    assert exits[0].timestamp == pytest.approx(T0 + 1.0)


def test_dwell_exit_is_not_repeated_after_it_fires():
    cfg = GateConfig(lines=[ENTRY_LINE], zone=ZONE, dwell_exit_s=1.0, track_timeout_s=60.0)
    logic = GateLogic(cfg, "gate_cam1")
    logic.update([car(0.5, 0.9)], T0, FRAME)
    assert len(logic.prune(T0 + 1.5)) == 1
    assert logic.prune(T0 + 30.0) == []


# -- dead tracks and reused ids ----------------------------------------------
def test_expired_track_history_lets_a_reused_id_fire_again():
    """ByteTrack recycles ids; a new vehicle must not inherit the old one's
    'already entered' mark."""
    cfg = GateConfig(lines=[ENTRY_LINE], zone=[], track_timeout_s=2.0, dwell_exit_s=60.0)
    logic = GateLogic(cfg, "gate_cam1")
    assert len(drive(logic, [(0.5, 0.7), (0.5, 0.9)])) == 1
    logic.prune(T0 + 5.0)  # the first car is long gone
    again = drive(logic, [(0.5, 0.7), (0.5, 0.9)], start_t=T0 + 6.0)
    assert [c.role for c in again] == [ENTRY]


def test_a_reacquired_track_keeps_its_single_entry():
    """Lost for less than the timeout, then seen again: still one event."""
    cfg = GateConfig(lines=[ENTRY_LINE], zone=[], track_timeout_s=5.0)
    logic = GateLogic(cfg, "gate_cam1")
    assert len(logic.update([car(0.5, 0.7)], T0, FRAME)) == 0
    assert len(logic.update([car(0.5, 0.9)], T0 + 0.1, FRAME)) == 1
    logic.prune(T0 + 2.0)  # gap, within the timeout
    assert logic.update([car(0.5, 0.7), car(0.5, 0.9)], T0 + 2.5, FRAME) == []
