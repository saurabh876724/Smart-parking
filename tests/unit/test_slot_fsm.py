"""FSM tests: the debounce behaviour that keeps the slot map from flickering."""

from __future__ import annotations

from smart_parking.core.config import OccupancyConfig
from smart_parking.core.schemas import SlotStatus
from smart_parking.parking.slot_fsm import SlotEvidence, SlotFSM

T0 = 1000.0


def make_fsm(**over) -> SlotFSM:
    cfg = OccupancyConfig(occupied_after_s=0.5, free_after_s=8.0, occupy_score=0.45)
    identity_stable = over.pop("identity_stable", True)
    for key, value in over.items():
        setattr(cfg, key, value)
    return SlotFSM("A1", cfg, identity_stable=identity_stable)


def ev(track=1, score=0.9):
    return SlotEvidence(track_id=track, score=score)


def test_starts_free():
    fsm = make_fsm()
    assert fsm.status is SlotStatus.FREE


def test_occupies_only_after_dwell():
    fsm = make_fsm()
    assert fsm.update(ev(), T0) is None
    assert fsm.status is SlotStatus.PENDING_OCCUPIED
    assert fsm.update(ev(), T0 + 0.2) is None
    assert fsm.status is SlotStatus.PENDING_OCCUPIED
    assert fsm.update(ev(), T0 + 0.6) is SlotStatus.OCCUPIED


def test_single_frame_blip_never_occupies():
    """A detection that appears for one frame must not flip a slot."""
    fsm = make_fsm()
    fsm.update(ev(), T0)
    assert fsm.update(None, T0 + 0.1) is None
    assert fsm.status is SlotStatus.FREE


def test_passing_car_does_not_claim_slot():
    """A vehicle crossing the bay for shorter than the dwell time is ignored."""
    fsm = make_fsm()
    for step in range(4):
        fsm.update(ev(score=0.95), T0 + step * 0.1)
    assert fsm.status is SlotStatus.PENDING_OCCUPIED
    for step in range(4, 8):
        fsm.update(None, T0 + step * 0.1)
    assert fsm.status is SlotStatus.FREE
    assert fsm.history == []


def test_departure_waits_the_long_free_timer():
    fsm = make_fsm()
    for step in range(10):
        fsm.update(ev(), T0 + step * 0.1)
    assert fsm.status is SlotStatus.OCCUPIED

    assert fsm.update(None, T0 + 1.0) is None
    assert fsm.status is SlotStatus.PENDING_FREE
    # Still occupied 7 s later: a false FREE would double-book the slot.
    assert fsm.update(None, T0 + 8.0) is None
    assert fsm.status is SlotStatus.PENDING_FREE
    assert fsm.update(None, T0 + 9.2) is SlotStatus.FREE
    assert fsm.track_id is None


def test_return_during_pending_free_cancels_it():
    fsm = make_fsm()
    for step in range(10):
        fsm.update(ev(), T0 + step * 0.1)
    fsm.update(None, T0 + 1.0)
    assert fsm.status is SlotStatus.PENDING_FREE
    assert fsm.update(ev(), T0 + 2.0) is None
    assert fsm.status is SlotStatus.OCCUPIED
    # Only one occupancy transition happened, so billing sees one session.
    assert [s for _, s in fsm.history] == [SlotStatus.OCCUPIED]


def test_new_track_resets_the_dwell_timer():
    """A different vehicle in the same slot is re-proven from scratch."""
    fsm = make_fsm()
    for step in range(10):
        fsm.update(ev(track=1), T0 + step * 0.1)
    assert fsm.status is SlotStatus.OCCUPIED
    fsm.update(None, T0 + 1.0)
    assert fsm.update(ev(track=2), T0 + 1.1) is None
    assert fsm.status is SlotStatus.PENDING_FREE or fsm.track_id == 2
    assert fsm.update(ev(track=2), T0 + 1.2) is None
    assert fsm.status is SlotStatus.OCCUPIED


def test_unstable_ids_do_not_reset_the_dwell_timer():
    """Without tracking, ids change every frame; debouncing must still work."""
    churn = make_fsm(identity_stable=False)
    for step in range(10):
        churn.update(ev(track=100 + step), T0 + step * 0.1)
    assert churn.status is SlotStatus.OCCUPIED

    # A tracked FSM on the same evidence never gets there, by design.
    tracked = make_fsm()
    for step in range(10):
        tracked.update(ev(track=100 + step), T0 + step * 0.1)
    assert tracked.status is SlotStatus.PENDING_OCCUPIED


def test_score_below_threshold_is_not_evidence():
    fsm = make_fsm()
    for step in range(20):
        fsm.update(ev(score=0.40), T0 + step * 0.1)
    assert fsm.status is SlotStatus.FREE


def test_force_unknown_blocks_transitions_until_recovered():
    fsm = make_fsm()
    for step in range(10):
        fsm.update(ev(), T0 + step * 0.1)
    assert fsm.force(SlotStatus.UNKNOWN, T0 + 1.0) is True
    assert fsm.update(ev(), T0 + 1.1) is None
    assert fsm.status is SlotStatus.UNKNOWN

    # A camera coming back must re-prove occupancy, not restore the old state.
    assert fsm.recover(ev(), T0 + 2.0) is None
    assert fsm.status is SlotStatus.PENDING_OCCUPIED
    assert fsm.update(ev(), T0 + 2.6) is SlotStatus.OCCUPIED
