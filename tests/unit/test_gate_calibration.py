"""Gate calibration tool (Phase 6): what the mouse writes, the loader must read.

The drawing itself needs a window and a camera snapshot, so what is testable head
and what matters most is the file: geometry normalised to 0-1, roles and
directions the config layer accepts, and a save that `load_gate_lines` reads back
without a second format anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from smart_parking.core.config import GateConfig, gate_config_for, load_gate_lines, load_config
from smart_parking.parking.calibration import GateCollector

FRAME = np.zeros((360, 480, 3), np.uint8)
ZONE_CLICKS = ((48, 36), (432, 36), (432, 270), (48, 270))


def drawn(*, zone_points: int = 0) -> GateCollector:
    """One bidirectional lane: the same segment drawn twice, facing opposite ways."""
    c = GateCollector(FRAME)
    c.start_line("ENTRY")
    c.add_point(240, 36)
    c.add_point(240, 270)
    c.start_line("EXIT")
    c.add_point(240, 36)
    c.add_point(240, 270)
    c.flip_direction()
    if zone_points:
        c.start_zone()
        for x, y in ZONE_CLICKS[:zone_points]:
            c.add_point(x, y)
    return c


# -- capture -----------------------------------------------------------------
def test_two_clicks_make_one_line_named_for_its_role():
    c = GateCollector(FRAME)
    c.start_line("ENTRY")
    assert c.add_point(240, 36) is None  # the first click is only half a line
    assert c.add_point(240, 270) == "line"
    c.start_line("EXIT")
    c.add_point(10, 10)
    c.add_point(20, 20)
    assert [(l.name, l.role) for l in c.lines] == [("in1", "ENTRY"), ("out1", "EXIT")]


def test_a_click_with_nothing_being_drawn_records_nothing():
    c = GateCollector(FRAME)
    assert c.add_point(100, 100) is None
    assert c.lines == [] and c.zone == []


def test_points_are_normalised_so_the_drawing_survives_a_resolution_change():
    small = GateCollector(np.zeros((360, 480, 3), np.uint8))
    large = GateCollector(np.zeros((1080, 1440, 3), np.uint8))
    for collector, w, h in ((small, 480, 360), (large, 1440, 1080)):
        collector.start_line("ENTRY")
        collector.add_point(w // 2, int(h * 0.1))
        collector.add_point(w // 2, int(h * 0.75))
    assert small.lines[0].points == large.lines[0].points


def test_flip_direction_only_touches_the_last_line():
    c = drawn(zone_points=0)
    assert [l.direction for l in c.lines] == ["positive", "negative"]
    c.flip_direction()
    assert [l.direction for l in c.lines] == ["positive", "positive"]


def test_undo_drops_the_half_drawn_point_and_zone_drops_its_last():
    c = GateCollector(FRAME)
    c.start_line("ENTRY")
    c.add_point(240, 36)
    c.undo()
    assert c.add_point(240, 270) is None  # back to needing two clicks
    c.start_zone()
    for x, y in ((48, 36), (432, 36), (432, 270)):
        c.add_point(x, y)
    c.undo()
    assert len(c.zone) == 2


def test_delete_last_removes_a_line_then_the_zone():
    c = drawn(zone_points=4)
    c.close_zone()
    c.delete_last()
    c.delete_last()
    assert len(c.lines) == 0
    c.delete_last()
    assert c.zone == []


def test_a_two_point_zone_is_refused_when_it_is_closed():
    c = GateCollector(FRAME)
    c.start_zone()
    c.add_point(48, 36)
    c.add_point(432, 36)
    assert c.close_zone() is False
    c.start_zone()
    c.add_point(432, 270)
    assert c.close_zone() is True


# -- the file it writes ------------------------------------------------------
def test_the_drawn_geometry_validates_as_a_gate_config():
    c = drawn(zone_points=4)
    c.close_zone()
    cfg = GateConfig(**c.to_config("gate_cam1"))
    assert [(l.name, l.role, l.direction) for l in cfg.lines] == [
        ("in1", "ENTRY", "positive"),
        ("out1", "EXIT", "negative"),
    ]
    assert cfg.zone[0] == [0.1, 0.1]


def test_a_saved_gate_file_reads_back_through_the_loader(tmp_path):
    c = drawn(zone_points=4)
    c.close_zone()
    path = tmp_path / "configs" / "gates" / "gate_cam1.json"
    c.save(path, "gate_cam1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["coordinate_space"] == "normalised_0_1"
    loaded = load_gate_lines(path, tmp_path)
    assert [line["name"] for line in loaded["lines"]] == ["in1", "out1"]
    assert len(loaded["zone"]) == 4


def test_the_saved_file_picks_up_the_global_timers(tmp_path):
    (tmp_path / "configs").mkdir(parents=True)
    (tmp_path / "configs" / "app.yaml").write_text(
        "gate:\n  dwell_exit_s: 9.0\n  min_ocr_confidence: 0.7\n", encoding="utf-8"
    )
    (tmp_path / "configs" / "cameras.yaml").write_text(
        "cameras:\n  - id: gate_cam1\n    source: x.mp4\n"
        "    gate_file: configs/gates/gate_cam1.json\n",
        encoding="utf-8",
    )
    collector = drawn(zone_points=4)
    collector.close_zone()
    collector.save(tmp_path / "configs" / "gates" / "gate_cam1.json", "gate_cam1")

    cfg = load_config(root=tmp_path)
    gate = gate_config_for(cfg, cfg.cameras[0])
    assert gate.dwell_exit_s == 9.0
    assert gate.min_ocr_confidence == 0.7
    assert len(gate.lines) == 2


def test_an_empty_drawing_loads_as_an_inert_stage_and_a_malformed_line_does_not():
    """Empty geometry is loadable but inert - the stage stays off, not guessed."""
    c = GateCollector(FRAME)
    cfg = GateConfig(**c.to_config("gate_cam1"))
    assert cfg.lines == []
    broken = c.to_config("gate_cam1")
    broken["lines"] = [{"name": "in1"}]  # no role, no points
    with pytest.raises(ValidationError):
        GateConfig(**broken)


# -- the canvas -------------------------------------------------------------
def test_rendering_the_canvas_needs_no_window():
    c = drawn(zone_points=4)
    canvas = c.render()
    assert canvas.shape == FRAME.shape
    assert not np.array_equal(canvas, FRAME)  # something was actually drawn
