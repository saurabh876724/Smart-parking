"""Plate normalisation and validation tests (Phase 5, spec 10 A/B)."""

from __future__ import annotations

import pytest

from smart_parking.anpr.validation import PlateValidator
from smart_parking.core.config import AnprValidationConfig

TYPICAL = [
    "MH20AB1234",
    "TS09DP4567",
    "DL12CD8901",
    "AS01N1234",      # single series letter
    "KL07ABC1234",    # three series letters
    "AB12BH3456",     # Bharat series
    "MH123456",       # no series letters at all
]

JUNK = [
    "Hello World",
    "PARKING",
    "1234",
    "",
    "MH20AB123456789012",  # longer than any plate
    "OUT",
    "TODO",
]


def validator(**over) -> PlateValidator:
    settings = dict(over)
    cfg = AnprValidationConfig(**settings)
    return PlateValidator(cfg)


# ---------------------------------------------------------------- normalisation

@pytest.mark.parametrize("text", ["mh20ab1234", "MH 20 AB 1234", "MH-20-AB-1234", "MH/20 AB 1234"])
def test_formatting_variants_normalise_to_one_plate(text):
    assert validator().normalise(text) == "MH20AB1234"


def test_lowercase_input_becomes_uppercase():
    assert validator().validate("mh20ab1234").plate == "MH20AB1234"


def test_devanagari_digits_are_translated_not_guessed():
    # Real regional plates carry Devanagari numerals; ०१२३४५६७८९ map exactly.
    assert validator().validate("MH२०AB१२३४").plate == "MH20AB1234"


def test_empty_text_normalises_to_empty():
    outcome = validator().validate("")
    assert outcome.plate is None
    assert outcome.reason == "empty_after_normalisation"


def test_printed_country_marker_is_stripped_from_the_edge():
    # High-security plates print INDIA on the plate itself, so OCR returns it.
    assert validator().validate("INDIA MH20AB1234").plate == "MH20AB1234"
    assert validator().validate("MH20AB1234BHARAT").plate == "MH20AB1234"


def test_country_word_inside_the_text_is_not_stripped():
    # Only the edges are touched: stripping mid-string could eat real characters.
    v = validator()
    assert v.normalise("MHINDIA20AB") == "MHINDIA20AB"
    assert v.normalise("INDIAMH20AB1234") == "MH20AB1234"


# -------------------------------------------------------------------- validation

@pytest.mark.parametrize("plate", TYPICAL)
def test_legitimate_series_are_accepted(plate):
    outcome = validator().validate(plate)
    assert outcome.plate == plate, outcome.reason
    assert outcome.edits == 0


@pytest.mark.parametrize("text", JUNK)
def test_obviously_invalid_text_is_rejected(text):
    assert validator().validate(text).plate is None


def test_old_central_government_series_is_accepted():
    outcome = validator().validate("23AH1234")
    assert outcome.plate == "23AH1234"
    assert outcome.pattern == "central_old"


def test_partial_plate_below_minimum_length_is_rejected():
    # MH20AB123 is a plausible truncation, and a truncation is not a plate.
    assert validator().validate("MH20AB123").plate is None


def test_a_plate_that_merely_contains_a_valid_shape_is_rejected():
    assert validator().validate("PARKMH20AB1234X").plate is None


def test_extra_pattern_from_config_is_accepted():
    v = validator(extra_patterns=[r"[A-Z]{3}[0-9]{6}"])
    assert v.validate("TRC123456").plate == "TRC123456"


# ---------------------------------------------------------------------- repair

def test_letter_read_as_digit_is_repaired():
    outcome = validator().validate("MH20ABL234")
    assert outcome.plate == "MH20AB1234"
    assert outcome.edits == 1


def test_repair_never_changes_the_length():
    plain = validator()
    for text in ["MH20ABL234", "MH2OAB1234", "TS09DP4S67", "MH20A81234"]:
        outcome = plain.validate(text)
        if outcome.plate is not None:
            assert len(outcome.plate) == len(plain.normalise(text))


def test_repair_is_bounded_by_the_configured_edit_count():
    # Two characters need re-interpreting here, so the default budget of one
    # refuses. Widening the budget accepts - and the result is a *different*
    # plausible plate, MH2OAB1235, that no camera necessarily photographed.
    # That is exactly the invented answer the tight default exists to prevent.
    text = "MH2OABI23S"
    assert validator().validate(text).plate is None
    widened = validator(max_repair_edits=2).validate(text)
    assert widened.plate == "MH2OAB1235"
    assert widened.edits == 2
    # Repairs are tried at the smallest edit count first, so a generous budget
    # never jumps over a cheaper match.
    assert validator(max_repair_edits=3).validate(text).edits == 2


def test_short_reading_is_not_repaired_into_a_shorter_plate():
    # 'MH20AB123' is a plausible truncation of MH20AB1234. Turning 'B' into '8'
    # would make it *match* a format - as a number that was never issued.
    assert validator().validate("MH20AB123").plate is None
    # A full-length read of the same shape is repairable.
    assert validator().validate("MH20ABL234").plate == "MH20AB1234"


def test_repair_length_guard_is_configurable():
    assert validator(repair_lengths=[9]).validate("MH20AB123").plate == "MH20A8123"


def test_repair_can_be_switched_off():
    assert validator(repair_confusables=False).validate("MH20ABL234").plate is None


def test_letter_positions_are_not_repaired_by_default():
    # 'B' read as '8': fixing a digit into a letter slot is more likely to
    # corrupt a real plate than to rescue this one, so it is opt-in.
    assert validator().validate("MH20A81234").plate is None
    assert validator(repair_letter_positions=True).validate("MH20A81234").plate == "MH20AB1234"


def test_the_grammar_chooses_between_two_plausible_readings():
    # 'B' is a legal series letter, but at that position the plate would need
    # four trailing digits, so only the digit reading forms a valid number at
    # all. Substitution is driven by the format, never by a character table
    # applied blindly.
    outcome = validator().validate("TS09DP4B67")
    assert outcome.plate == "TS09DP4867"
    assert outcome.edits == 1


# ----------------------------------------------------------------- region codes

def test_region_code_check_is_off_by_default():
    assert validator().validate("ZZ20AB1234").plate == "ZZ20AB1234"


def test_region_code_check_rejects_unknown_state_when_required():
    assert validator(require_region_code=True).validate("ZZ20AB1234").plate is None
    assert validator(require_region_code=True).validate("MH20AB1234").plate == "MH20AB1234"


def test_region_code_check_keeps_old_central_plates():
    # Those begin with digits and have no region code to check.
    assert validator(require_region_code=True).validate("23AH1234").plate == "23AH1234"
