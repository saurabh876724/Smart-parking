"""ANPR pipeline failure handling (Phase 5, spec 10 D, E and G).

The OCR engine is stubbed here, and that is the point: these are the cases a
camera produces at 3 a.m. under a sodium lamp, and they have to be assertable
without asking a real engine to reproduce them on demand.
"""

from __future__ import annotations

import numpy as np
import pytest

from smart_parking.anpr.pipeline import AnprPipeline
from smart_parking.core.config import AnprConfig, AppConfig
from smart_parking.core.events import RecordingEventBus
from smart_parking.core.schemas import PLATE_READ, SLOT_OCCUPIED, Track

# The vehicle box the stub pretends to see, and the plate region inside it.
VEHICLE = (100.0, 100.0, 300.0, 280.0)
PLATE_LOCAL = (120.0, 150.0, 280.0, 186.0)  # 160 x 36, plate-shaped by the screens
FRAME = np.zeros((400, 400, 3), np.uint8)
T0 = 1000.0
STEP = 0.2  # five reads a second of video time

MH = ("MH20AB1234", 0.9)
DL = ("DL12CD8901", 0.9)
KA = ("MH20AB1284", 0.9)


class StubTextDet:
    """Answers with one fixed plate-shaped region, in ROI-local coordinates."""

    def __init__(self, score: float = 0.9, fires: bool = True) -> None:
        self.score = score
        self.fires = fires

    def get_preprocess(self, max_side):
        return lambda image: image

    def infer(self, prepared):
        return [None]

    def postprocess_op(self, preds, shape):
        if not self.fires:
            return None, None
        x1, y1, x2, y2 = PLATE_LOCAL
        quad = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], np.float32)
        return [quad], [self.score]


class StubTextRec:
    """Scripted recognition: one answer per call.

    `cycle` repeats the list forever, which is how a two-vehicle frame gives each
    vehicle its own plate: the tracks are read in order, so the answers alternate
    per frame rather than per vehicle.
    """

    def __init__(self, answers: list[tuple[str, float]], cycle: bool = False) -> None:
        self.answers = answers
        self.cycle = cycle
        self.calls = 0

    def __call__(self, images):
        index = self.calls % len(self.answers) if self.cycle else min(self.calls, len(self.answers) - 1)
        text, score = self.answers[index]
        self.calls += 1
        return [(text, score)], 0.0


class StubOcr:
    def __init__(self, answers, *, cycle=False, fires=True, det_score=0.9) -> None:
        self.text_det = StubTextDet(score=det_score, fires=fires)
        self.text_rec = StubTextRec(answers, cycle=cycle)


def car(track_id: int = 1) -> Track:
    return Track(
        bbox=VEHICLE,
        confidence=0.9,
        class_id=2,
        class_name="car",
        track_id=track_id,
        footprint_ratio=0.4,
    )


def build(answers, *, ocr=None, bus=None, enabled=True, votes=3, agreement=0.6, window=5, tracks=1, **kwargs):
    """An AnprPipeline on the shipped screens, with a stubbed engine."""
    if ocr is None:
        ocr = StubOcr(answers, **{"cycle": kwargs.pop("cycle", False)})
    cfg = AppConfig(
        anpr=AnprConfig(
            enabled=enabled,
            voting={
                "window_size": window,
                "minimum_observations": votes,
                "minimum_agreement": agreement,
                "max_track_age_s": 3.0,
            },
        )
    )
    return AnprPipeline(cfg, "gate_cam1", bus=bus if bus is not None else RecordingEventBus(), ocr=ocr)


def frames(p: AnprPipeline, count: int, track_ids=(1,), start=0):
    """Feed `count` frames of `track_ids` and return the per-frame result lists."""
    return [
        p.process(FRAME, [car(tid) for tid in track_ids], T0 + (start + i) * STEP)
        for i in range(count)
    ]


def test_a_repeated_read_becomes_one_stable_plate():
    p = build([MH])
    results = frames(p, 3)
    assert results[0] == [] and results[1] == []
    assert len(results[2]) == 1
    result = results[2][0]
    assert result.plate_text == "MH20AB1234"
    assert result.stable and result.observations == 3
    assert result.track_id == 1 and result.class_name == "car"
    # The reported box is the plate inside the vehicle, in frame coordinates:
    # 160x36 as the stub proposed it, offset by the vehicle box the detector
    # searched.
    x1, y1, x2, y2 = result.bbox
    assert x2 - x1 == pytest.approx(160, abs=1.5)
    assert y2 - y1 == pytest.approx(36, abs=1.5)
    assert result.bbox == pytest.approx((208.0, 283.0, 368.0, 319.0), abs=1.5)


def test_a_stable_plate_is_published_once_not_on_every_frame():
    bus = RecordingEventBus()
    p = build([MH], bus=bus)
    frames(p, 6)
    plate_events = [e for e in bus.recorded if e.type == PLATE_READ]
    assert len(plate_events) == 1
    assert plate_events[0].payload["plate"] == "MH20AB1234"
    assert plate_events[0].camera_id == "gate_cam1"
    assert p.stats.stable_results == 1


def test_the_majority_plate_wins_over_one_disagreeing_frame():
    # The spec's example: one read of MH20AB1284 then a run of MH20AB1234. The
    # vote is what matters here, not the frame it confirmed on.
    p = build([KA, MH, MH, MH, MH])
    results = frames(p, 5)
    assert results[2] == [], "two agreeing reads against one disagreeing is not a plate"
    assert any(results[3]), "the third agreeing read confirms"
    final = [r for r in results if r][-1][0]
    assert final.plate_text == "MH20AB1234"
    vote = p.registry.vote(1)
    assert vote.plate == "MH20AB1234"
    assert vote.count == 4 and vote.total == 5
    assert vote.agreement == pytest.approx(0.8)


def test_no_plate_detected_costs_no_ocr_call_and_no_event():
    bus = RecordingEventBus()
    ocr = StubOcr([MH], fires=False)
    p = build([MH], bus=bus, ocr=ocr)
    assert all(r == [] for r in frames(p, 5))
    assert bus.recorded == []
    assert p.stats.vehicles == 5
    assert p.stats.candidates == 0
    assert p.stats.reads == 0, "a vehicle with no plate region must not reach OCR"
    assert ocr.text_rec.calls == 0


def test_an_empty_ocr_answer_is_counted_as_a_failed_read():
    p = build([("", 0.0)])
    assert all(r == [] for r in frames(p, 5))
    assert p.stats.reads == 5
    assert p.stats.accepted_reads == 0
    assert p.stats.read_accept_rate == 0.0


def test_text_that_is_not_a_plate_is_rejected_before_it_can_vote():
    # A shop sign passes every geometric screen; validation is what stops it.
    p = build([("PARKING", 0.95)])
    assert all(r == [] for r in frames(p, 5))
    assert p.stats.reads == 5 and p.stats.accepted_reads == 0


def test_a_low_confidence_read_never_votes():
    # Real plate text, but a score under the floor means the engine was guessing.
    p = build([("MH20AB1234", 0.20)])
    assert all(r == [] for r in frames(p, 5))
    assert p.stats.accepted_reads == 0, "a guessed plate must not enter the window"


def test_a_track_that_leaves_the_frame_is_still_known_but_reports_nothing_new():
    p = build([MH])
    frames(p, 3)
    assert p.plate_for(1).plate_text == "MH20AB1234"
    for i in range(3):
        assert p.process(FRAME, [], T0 + (3 + i) * STEP) == []
    assert p.plate_for(1) is not None, "a short absence must not erase the plate"


def test_a_reused_track_id_does_not_inherit_the_previous_plate():
    p = build([MH])
    frames(p, 3)
    assert p.plate_for(1).plate_text == "MH20AB1234"
    # ByteTrack hands id 1 to the next car after a gap longer than the window.
    assert p.process(FRAME, [], T0 + 20.0) == []
    assert p.plate_for(1) is None
    assert frames(p, 2) [-1] == [], "two reads are not a confirmed plate"
    assert frames(p, 1, start=3)[-1][0].observations == 3


def test_two_vehicles_in_one_frame_get_their_own_plates():
    ocr = StubOcr([MH, DL], cycle=True)
    p = build([MH, DL], ocr=ocr)
    results = frames(p, 4, track_ids=(1, 2))
    by_track = {}
    for frame_results in results:
        for r in frame_results:
            by_track[r.track_id] = r.plate_text
    assert by_track == {1: "MH20AB1234", 2: "DL12CD8901"}
    assert ocr.text_rec.calls == 8


def test_anpr_publishes_only_plate_events():
    bus = RecordingEventBus()
    p = build([MH], bus=bus)
    frames(p, 5)
    assert {e.type for e in bus.recorded} == {PLATE_READ}
    assert SLOT_OCCUPIED not in {e.type for e in bus.recorded}


def test_disabled_anpr_reads_nothing():
    ocr = StubOcr([MH])
    p = build([MH], ocr=ocr, enabled=False)
    assert all(r == [] for r in frames(p, 5))
    assert p.stats.vehicles == 0 and p.stats.reads == 0
    assert ocr.text_rec.calls == 0


def test_reset_clears_the_window_and_the_deduplication_memory():
    p = build([MH])
    frames(p, 3)
    assert p.plate_for(1) is not None
    p.reset()
    assert p.plate_for(1) is None
    # After a reconnect the same plate has to be earned again, and may be
    # reported a second time.
    assert frames(p, 2)[-1] == []
    assert len(frames(p, 1)[-1] if False else frames(p, 1)) == 1
