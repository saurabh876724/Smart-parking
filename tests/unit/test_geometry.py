"""Geometry tests: normalisation round-trip, point-in-polygon, overlap."""

from __future__ import annotations

import pytest

from smart_parking.core.schemas import Point
from smart_parking.parking.geometry import (
    containment_score,
    overlap_score,
    point_in_polygon,
    polygon_area,
    slots_from_config,
    to_normalised,
    to_pixels,
)

SQUARE = (Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100))


def test_polygon_area():
    assert polygon_area(SQUARE) == pytest.approx(10_000)


def test_point_in_polygon():
    assert point_in_polygon(Point(50, 50), SQUARE)
    assert not point_in_polygon(Point(150, 50), SQUARE)
    assert not point_in_polygon(Point(-1, 50), SQUARE)


def test_normalisation_round_trip():
    normalised = (Point(0.0, 0.0), Point(0.5, 0.0), Point(0.5, 0.5), Point(0.0, 0.5))
    pixels = to_pixels(normalised, 1920, 1080)
    assert (pixels[1].x, pixels[1].y) == pytest.approx((960, 0))
    back = to_normalised(pixels, 1920, 1080)
    assert [(p.x, p.y) for p in back] == pytest.approx([(p.x, p.y) for p in normalised])


def test_overlap_score_is_slot_denominated():
    # A box covering the left half of the slot.
    assert overlap_score((0, 0, 50, 100), SQUARE) == pytest.approx(0.5)


def test_containment_score_is_footprint_denominated():
    # The same box seen from the vehicle's side: it is fully inside the slot.
    assert containment_score((0, 0, 50, 100), SQUARE) == pytest.approx(1.0)


def test_small_car_in_big_slot():
    """The doc's metric scores a perfectly-parked small far-row car ~0.2, which
    is why assignment uses containment instead. This pins that decision."""
    big_slot = (Point(0, 0), Point(400, 0), Point(400, 400), Point(0, 400))
    footprint = (150, 150, 250, 250)
    assert overlap_score(footprint, big_slot) < 0.10
    assert containment_score(footprint, big_slot) == pytest.approx(1.0)


def test_vehicle_in_aisle_scores_zero_everywhere():
    assert containment_score((500, 500, 600, 600), SQUARE) == pytest.approx(0.0)
    assert overlap_score((500, 500, 600, 600), SQUARE) == pytest.approx(0.0)


def test_partial_containment():
    # Half the footprint (x 50-150 vs slot 0-100) sits inside.
    assert containment_score((50, 0, 150, 100), SQUARE) == pytest.approx(0.5)


def test_slots_from_config_normalised():
    entries = [
        {"code": "A1", "polygon": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]},
        {"code": "A2", "polygon": [[0.5, 0.0], [1.0, 0.0], [1.0, 0.5], [0.5, 0.5]]},
    ]
    slots = slots_from_config(entries)
    assert set(slots) == {"A1", "A2"}
    assert slots["A2"][2] == Point(1.0, 0.5)


def test_slots_from_config_rejects_duplicates():
    dup = [{"code": "A1", "polygon": [[0, 0], [1, 0], [1, 1]]}] * 2
    with pytest.raises(ValueError, match="duplicate"):
        slots_from_config(dup)
