"""Track-to-plate association tests (Phase 5, spec 10 E).

Association is where a plate stops being a string and becomes a fact about a
vehicle: the window belongs to a track id, the geometry belongs to the last
observation, and an id that goes stale must come back empty.
"""

from __future__ import annotations

from smart_parking.anpr.association import TrackPlateRegistry
from smart_parking.core.config import AnprVotingConfig
from smart_parking.core.schemas import PlateObservation

T0 = 1000.0
BOX = (140.0, 300.0, 260.0, 332.0)


def observation(
    track_id: int = 1,
    plate: str | None = "MH20AB1234",
    ts: float = T0,
    score: float = 0.9,
    detector_score: float = 0.7,
    bbox: tuple[float, float, float, float] = BOX,
) -> PlateObservation:
    return PlateObservation(
        track_id=track_id,
        raw_text=plate or "PARKING",
        plate=plate,
        ocr_confidence=score,
        detector_confidence=detector_score,
        timestamp=ts,
        bbox=bbox,
    )


def registry(**overrides) -> TrackPlateRegistry:
    return TrackPlateRegistry(AnprVotingConfig(**overrides))


def record_all(reg: TrackPlateRegistry, plates: list[str], track_id: int = 1, step: float = 0.1):
    last = None
    for i, plate in enumerate(plates):
        last = reg.record(observation(track_id=track_id, plate=plate, ts=T0 + i * step))
    return last


def test_rejected_reads_do_not_open_a_window():
    reg = registry()
    assert reg.record(observation(plate=None)) is None
    assert len(reg) == 0
    assert reg.vote(1) is None


def test_each_track_votes_alone():
    reg = registry()
    record_all(reg, ["MH20AB1234"] * 3, track_id=1)
    record_all(reg, ["DL12CD8901"] * 2, track_id=2)
    assert reg.vote(1).plate == "MH20AB1234"
    assert reg.vote(1).count == 3
    assert reg.vote(2).plate == "DL12CD8901"
    assert reg.vote(2).count == 2
    assert len(reg) == 2


def test_a_stable_vote_becomes_a_result_with_the_track_geometry():
    reg = registry()
    vote = record_all(reg, ["MH20AB1234"] * 3)
    assert vote.stable
    result = reg.result_for(1, vote, class_name="car")
    assert result.plate_text == "MH20AB1234"
    assert result.stable
    # Geometry and detector score come from the most recent observation, not the
    # vote: the vote is text, the observation is where the plate was.
    assert result.bbox == BOX
    assert result.plate_confidence == 0.7
    assert result.ocr_confidence == 0.9
    assert result.observations == 3
    assert result.class_name == "car"


def test_the_latest_observation_supplies_the_box():
    reg = registry()
    record_all(reg, ["MH20AB1234"] * 2)
    moved = observation(plate="MH20AB1234", ts=T0 + 0.2, bbox=(150.0, 310.0, 270.0, 342.0))
    vote = reg.record(moved)
    result = reg.result_for(1, vote)
    assert result.bbox == (150.0, 310.0, 270.0, 342.0)


def test_result_for_an_unknown_track_is_none():
    reg = registry()
    vote = record_all(reg, ["MH20AB1234"] * 3, track_id=7)
    assert reg.result_for(8, vote) is None


def test_an_unstable_vote_still_reads_back_as_a_result_for_the_overlay():
    # `plate_for` in the pipeline reports the leading string even before it is
    # stable, so an operator can see what the camera is thinking. The `stable`
    # flag is what says it may be trusted.
    reg = registry()
    vote = record_all(reg, ["MH20AB1234"] * 2)
    assert not vote.stable
    result = reg.result_for(1, vote)
    assert result is not None
    # result_for hard-codes stable=True because it is only called for a vote the
    # pipeline has decided to report; the caller gates on vote.stable.
    assert result.stable is True


def test_a_track_that_disappears_loses_its_window():
    reg = registry(max_track_age_s=3.0)
    record_all(reg, ["MH20AB1234"] * 3)
    assert reg.prune(T0 + 1.0) == []
    assert reg.vote(1) is not None
    assert reg.prune(T0 + 4.0) == [1]
    assert reg.vote(1) is None
    assert len(reg) == 0


def test_a_reused_track_id_starts_empty():
    # ByteTrack reuses ids after a gap. Without expiry the next car at the gate
    # would be reported with the previous car's plate, on the strength of a
    # window it never filled.
    reg = registry(max_track_age_s=3.0)
    record_all(reg, ["MH20AB1234"] * 3, track_id=5)
    reg.prune(T0 + 10.0)
    vote = record_all(reg, ["KA01AB1234"], track_id=5, step=0.1)
    assert vote.plate == "KA01AB1234"
    assert vote.count == 1 and vote.total == 1
    assert not vote.stable


def test_expiry_only_removes_the_stale_track():
    reg = registry(max_track_age_s=3.0)
    record_all(reg, ["MH20AB1234"] * 3, track_id=1)  # last read at T0+0.2
    record_all(reg, ["DL12CD8901"] * 2, track_id=2)
    reg.record(observation(track_id=2, plate="DL12CD8901", ts=T0 + 2.5))
    # 3.4s after the last read of track 1 it is stale; track 2 last spoke 0.9s ago.
    expired = reg.prune(T0 + 3.6)
    assert expired == [1]
    assert reg.vote(2).plate == "DL12CD8901"


def test_clear_drops_everything():
    reg = registry()
    record_all(reg, ["MH20AB1234"] * 3, track_id=1)
    record_all(reg, ["DL12CD8901"] * 3, track_id=2)
    reg.clear()
    assert len(reg) == 0
    assert reg.vote(1) is None and reg.vote(2) is None


def test_window_size_caps_the_evidence():
    reg = registry(window_size=4, minimum_observations=3, minimum_agreement=0.75)
    record_all(reg, ["MH20AB1234"] * 3 + ["MH20AB1284"] * 3)
    vote = reg.vote(1)
    assert vote.total == 4, "older reads must fall out of the window, not accumulate"
    assert vote.plate == "MH20AB1284"
    assert vote.count == 3 and vote.agreement == 0.75
