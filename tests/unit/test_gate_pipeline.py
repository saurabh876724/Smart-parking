"""Gate pipeline wiring (Phase 6, test group: the stages joined together).

The crossing rules and the session lifecycle have their own files; what is only
testable here is the seam between them - that a plate read reaches the session the
line creates, that the dwell timer still runs on a frame inference was skipped
for, and that a replay-driven run never asks for a detector.

No YOLO and no OCR engine: `track=False, anpr=False` builds the pipeline with the
heavy stages absent, and the plate stage is a stub that hands back a `PlateResult`
the same shape Phase 5 publishes.
"""

from __future__ import annotations

import numpy as np
import pytest

from smart_parking.core.config import AppConfig, CameraConfig, GateConfig
from smart_parking.core.events import RecordingEventBus
from smart_parking.core.schemas import (
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    Event,
    PlateResult,
    SessionStatus,
    Track,
)
from smart_parking.pipeline.gate_pipeline import GatePipeline

FRAME = (480, 360)
T0 = 500.0
GOOD_PLATE = "MH14AB1234"
IMAGE = np.zeros((FRAME[1], FRAME[0], 3), dtype=np.uint8)
IN_LINE = {"name": "in", "role": "ENTRY", "direction": "negative", "points": [[0.5, 0.1], [0.5, 0.9]]}
OUT_LINE = {"name": "out", "role": "EXIT", "direction": "positive", "points": [[0.5, 0.1], [0.5, 0.9]]}
ZONE = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]


def car(fx: float, fy: float, track_id: int = 1) -> Track:
    """A vehicle whose centroid sits at normalised (fx, fy) of ``FRAME``."""
    x, y = fx * FRAME[0], fy * FRAME[1]
    return Track(
        bbox=(x - 60.0, y - 30.0, x + 60.0, y + 30.0),
        confidence=1.0,
        class_id=2,
        class_name="car",
        track_id=track_id,
    )


def build(*, dwell_exit_s: float = 12.0, lines=None, zone=None, replay=None, track: bool = False):
    cfg = AppConfig()
    camera = CameraConfig(id="gate_cam1", name="Gate", source="does-not-open.mp4")
    gate = GateConfig(
        lines=lines if lines is not None else [IN_LINE, OUT_LINE],
        zone=ZONE if zone is None else zone,
        dwell_exit_s=dwell_exit_s,
    )
    bus = RecordingEventBus()
    sink: list[Event] = []
    pipeline = GatePipeline(
        cfg,
        camera,
        gate=gate,
        bus=bus,
        track=track,
        anpr=False,
        replay_tracks=replay,
        on_event=sink.append,
    )
    return pipeline, bus, sink


class FakeAnpr:
    """The Phase 5 stage as the pipeline sees it: results in, beliefs out."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.seen: dict[int, PlateResult] = {}
        self.calls = 0

    def process(self, frame, tracks, now):
        batch = self.batches[self.calls] if self.calls < len(self.batches) else []
        self.calls += 1
        for result in batch:
            self.seen[result.track_id] = result
        return batch

    def plate_for(self, track_id):
        return self.seen.get(track_id)


def read(track_id: int = 3, plate: str = GOOD_PLATE, ocr: float = 0.94, at: float = T0) -> PlateResult:
    return PlateResult(
        track_id=track_id, plate_text=plate, plate_confidence=0.7, ocr_confidence=ocr,
        timestamp=at, bbox=(240.0, 300.0, 360.0, 330.0), stable=True,
        observations=3, agreement=1.0, class_name="car",
    )


def session_of(pipeline: GatePipeline, track_id: int):
    return [s for s in pipeline.sessions.sessions.values() if s.track_id == track_id]


# -- a whole visit through the stages ----------------------------------------
def test_a_drive_in_and_back_out_makes_one_session_with_a_duration():
    pipeline, bus, sink = build()
    pipeline.anpr = FakeAnpr([read(track_id=1, at=T0)], [])
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.5)], T0 + 1.0, FRAME)
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0 + 30.0, FRAME)

    assert [e.type for e in bus.recorded] == [VEHICLE_ENTERED, VEHICLE_EXITED]
    assert [e.type for e in sink] == [VEHICLE_ENTERED, VEHICLE_EXITED]
    sessions = session_of(pipeline, 1)
    assert len(sessions) == 1
    assert sessions[0].status is SessionStatus.COMPLETED
    assert sessions[0].exit_reason == "line"
    assert sessions[0].duration_s == pytest.approx(29.0)


def test_a_re_crossed_entry_line_never_opens_a_second_session():
    """The gate refuses the repeat before the session layer has to."""
    pipeline, bus, _ = build(lines=[IN_LINE])
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.5)], T0 + 1.0, FRAME)
    # Back over it and in again: only one ENTRY per track is ever emitted.
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0 + 2.0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.5)], T0 + 3.0, FRAME)

    assert len(pipeline.sessions.sessions) == 1
    assert sum(pipeline.logic.duplicates.values()) == 1
    assert pipeline.sessions.duplicate_crossings == 0
    assert [e.type for e in bus.recorded] == [VEHICLE_ENTERED]


def test_a_vehicle_that_never_enters_the_zone_produces_nothing_at_all():
    pipeline, bus, _ = build()
    pipeline.process(IMAGE, [car(0.3, 0.05)], T0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.05)], T0 + 1.0, FRAME)

    assert bus.recorded == []
    assert pipeline.sessions.sessions == {}
    assert pipeline.crossings_seen == 0


# -- the dwell timer and the frame clock -------------------------------------
def test_a_vanished_vehicle_closes_on_a_frame_inference_was_skipped_for():
    """A stride skips the detector, not the clock: absence keeps accruing."""
    pipeline, bus, _ = build(dwell_exit_s=5.0)
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0, FRAME)
    for i in range(6):
        pipeline.process(IMAGE, [car(0.7, 0.5)], T0 + 1.0 + i, FRAME)
    assert pipeline.sessions.open_sessions()

    assert pipeline.tick(T0 + 8.0) == []  # gone 2s of a 5s timer
    events = pipeline.tick(T0 + 12.0)
    assert [e.type for e in events] == [VEHICLE_EXITED]
    session = session_of(pipeline, 1)[0]
    assert session.exit_reason == "dwell"
    # The visit ended when the car was last seen in the zone, not when the timer
    # gave up on it.
    assert session.exit_at == pytest.approx(T0 + 6.0)
    assert session.duration_s == pytest.approx(5.0)


# -- plate binding through the seam -----------------------------------------
def test_a_plate_read_before_the_line_is_borne_by_the_session_that_line_creates():
    pipeline, bus, _ = build()
    pipeline.anpr = FakeAnpr([read(track_id=3, at=T0)], [])
    pipeline.process(IMAGE, [car(0.3, 0.5, 3)], T0, FRAME)
    assert pipeline.sessions.sessions == {}

    pipeline.process(IMAGE, [car(0.7, 0.5, 3)], T0 + 1.0, FRAME)
    session = session_of(pipeline, 3)[0]
    assert session.plate == GOOD_PLATE
    assert bus.recorded[0].payload["plate"] == GOOD_PLATE


def test_a_low_confidence_read_survives_the_seam_as_a_review():
    pipeline, bus, _ = build()
    pipeline.anpr = FakeAnpr([read(track_id=1, ocr=0.42, at=T0 + 1.0)])
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.5)], T0 + 1.0, FRAME)
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0 + 20.0, FRAME)

    session = session_of(pipeline, 1)[0]
    assert session.status is SessionStatus.NEEDS_REVIEW
    assert "below" in (session.review_reason or "")


def test_the_overlay_labels_say_what_the_session_layer_believes():
    pipeline, _, _ = build()
    pipeline.anpr = FakeAnpr([read(track_id=9)], [])
    pipeline.process(IMAGE, [car(0.3, 0.5, 9)], T0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.5, 9)], T0 + 1.0, FRAME)
    labels = pipeline.labels([car(0.7, 0.5, 9)])
    assert GOOD_PLATE in labels[9]
    assert "00001" in labels[9]


def test_a_vehicle_with_no_read_is_labelled_as_having_no_read():
    pipeline, _, _ = build()
    pipeline.anpr = FakeAnpr([], [])
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.5)], T0 + 1.0, FRAME)
    assert pipeline.labels([car(0.7, 0.5)]) == {1: "no plate 00001"}


# -- what the constructor promises ------------------------------------------
def test_a_replay_driven_pipeline_never_builds_a_detector():
    """The fixture run must be runnable on a machine with no GPU in the loop.

    ``track=True`` is what the CLI passes for real footage; a labelled replay has
    to override it, or every fixture run would load YOLO to throw its output away.
    """
    pipeline, _, _ = build(replay=lambda frame: [car(0.4, 0.5)], track=True)
    assert pipeline.tracker is None
    pipeline.process(IMAGE, [car(0.7, 0.5)], T0, FRAME)
    assert pipeline.frames_processed == 1


def test_the_gate_config_is_shared_by_both_stages():
    pipeline, _, _ = build(dwell_exit_s=3.5)
    assert pipeline.logic.cfg is pipeline.sessions.cfg is pipeline.gate
    assert pipeline.gate.dwell_exit_s == 3.5


def test_a_pipeline_without_a_plate_stage_still_runs_sessions():
    """Crossings alone are a usable gate: plate reading is optional downstream."""
    pipeline, bus, _ = build()
    assert pipeline.anpr is None
    pipeline.process(IMAGE, [car(0.3, 0.5)], T0, FRAME)
    pipeline.process(IMAGE, [car(0.7, 0.5)], T0 + 1.0, FRAME)
    assert [e.type for e in bus.recorded] == [VEHICLE_ENTERED]
    session = session_of(pipeline, 1)[0]
    assert session.plate is None
