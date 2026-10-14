"""Multi-frame voting tests (Phase 5, spec 10 C and D).

The voter takes strings and scores, so these tests need no OCR engine and no
images: the consensus rule is the thing under test, and it is the rule that turns
a ~50%-accurate per-frame reader into a correct answer.
"""

from __future__ import annotations

import pytest

from smart_parking.anpr.voting import PlateVoter
from smart_parking.core.config import AnprVotingConfig

T0 = 1000.0
STEP = 0.1


def voter(**overrides) -> PlateVoter:
    valid = AnprVotingConfig.model_fields.keys()
    bad = set(overrides) - set(valid)
    if bad:
        raise TypeError(f"not AnprVotingConfig fields: {sorted(bad)}")
    return PlateVoter(AnprVotingConfig(**overrides))


def feed(v: PlateVoter, plates: list[str], start: float = T0, score: float = 0.9):
    """Add reads one timestep apart, returning the vote after each."""
    return [
        v.add(plate, score, start + i * STEP)
        for i, plate in enumerate(plates)
    ]


def test_one_read_is_never_stable():
    # A single crisp read is 100% agreement and still not an answer: agreement
    # alone would let one lucky frame confirm a plate.
    vote = voter().add("MH20AB1234", 0.99, T0)
    assert vote.agreement == 1.0
    assert not vote.stable
    assert vote.count == 1 and vote.total == 1


def test_repeated_read_becomes_stable_at_the_minimum_observation_count():
    v = voter(minimum_observations=3, minimum_agreement=0.6, window_size=7)
    votes = feed(v, ["MH20AB1234"] * 3)
    assert not votes[0].stable and not votes[1].stable
    assert votes[2].stable
    assert votes[2].count == 3 and votes[2].agreement == pytest.approx(1.0)


def test_the_spec_example_four_agreeing_reads_beat_one_disagreement():
    # Four ballots for MH20AB1234 against one for the single-character variant.
    v = voter(minimum_observations=3, minimum_agreement=0.6)
    final = feed(v, ["MH20AB1234", "MH20AB1284", "MH20AB1234", "MH20AB1234", "MH20AB1234"])[-1]
    assert final.plate == "MH20AB1234"
    assert final.count == 4 and final.total == 5
    assert final.agreement == pytest.approx(0.8)
    assert final.stable


def test_a_majority_below_the_agreement_floor_is_not_stable():
    # The same four correct reads, but a fifth of the window disagrees strongly
    # enough to sit under the 0.8 floor.
    v = voter(minimum_observations=3, minimum_agreement=0.8)
    final = feed(v, ["MH20AB1234"] * 4 + ["DL12CD8901"] * 2)[-1]
    assert final.plate == "MH20AB1234"
    assert final.count == 4 and final.total == 6
    assert final.agreement == pytest.approx(0.666, abs=0.001)
    assert not final.stable


def test_conflicting_reads_do_not_cancel_each_other_out():
    # Two plates read twice each: the vote must name a leader, not shrug.
    v = voter(minimum_observations=3, minimum_agreement=0.6)
    final = feed(v, ["MH20AB1234", "MH20AB1284", "MH20AB1234", "MH20AB1284"])[-1]
    assert final.count == 2
    assert not final.stable  # count is under the floor either way


def test_a_tie_breaks_on_mean_ocr_confidence():
    # Equal counts, so the higher-confidence plate leads. The tie-break is what
    # makes the result deterministic rather than dict-order dependent.
    v = voter()
    v.add("MH20AB1234", 0.60, T0)
    v.add("MH20AB1284", 0.95, T0 + STEP)
    assert v.current().plate == "MH20AB1284"


def test_an_equal_count_and_score_tie_breaks_on_recency():
    v = voter()
    v.add("MH20AB1234", 0.90, T0)
    v.add("MH20AB1284", 0.90, T0 + 5 * STEP)
    assert v.current().plate == "MH20AB1284"


def test_the_window_forgets_old_reads():
    # Window of 3: the original correct reads slide out as the wrong ones arrive,
    # which is why a vehicle that turns into a shop front stops voting for the
    # sign.
    v = voter(window_size=3, minimum_observations=2, minimum_agreement=0.6)
    feed(v, ["MH20AB1234"] * 3)
    assert v.current().plate == "MH20AB1234"
    final = feed(v, ["KA01AB1234"] * 3, start=T0 + 1.0)[-1]
    assert final.plate == "KA01AB1234"
    assert final.total == 3 and len(v) == 3


def test_mean_score_averages_only_the_agreeing_reads():
    v = voter()
    v.add("MH20AB1234", 0.90, T0)
    v.add("MH20AB1234", 0.80, T0 + STEP)
    v.add("MH20AB1284", 0.20, T0 + 2 * STEP)
    vote = v.current()
    assert vote.mean_score == pytest.approx(0.85)


def test_current_without_reads_is_none():
    assert voter().current() is None


def test_config_rejects_an_unreachable_or_nonsense_rule():
    with pytest.raises(ValueError):
        # minimum_agreement is a fraction of reads; 0.0 would let a single
        # read stand as consensus, which the spec forbids.
        AnprVotingConfig(minimum_agreement=0.0)
    with pytest.raises(ValueError):
        AnprVotingConfig(window_size=1)
    with pytest.raises(ValueError):
        AnprVotingConfig(minimum_observations=0)
