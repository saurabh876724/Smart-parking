"""Gate configuration chain (Phase 6).

Two rules the doc sets and this stage has to keep: geometry is per camera and
lives in its own file, timers are global and live in app.yaml, and no module
below ``core/config.py`` opens a YAML file by itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from smart_parking.core.config import (
    GateConfig,
    GateLineConfig,
    gate_config_for,
    load_config,
)
from smart_parking.parking.gate_logic import GateLogic

REPO_ROOT = Path(__file__).resolve().parents[2]
LINE = {"name": "in", "role": "ENTRY", "direction": "negative", "points": [[0.5, 0.15], [0.5, 0.7]]}
ZONE = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.75], [0.1, 0.75]]


def write_gate(tmp_path: Path, lines=None, zone=None) -> str:
    """Write a gate file the way the calibration tool does - as JSON."""
    folder = tmp_path / "configs" / "gates"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"camera_id": "gate_cam1", "lines": lines if lines is not None else [LINE]}
    if zone is not None:
        payload["zone"] = zone
    path = folder / "gate_a.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return "configs/gates/gate_a.json"


def load(tmp_path: Path, gate: dict | None, gate_file: str | None = None):
    app = tmp_path / "configs" / "app.yaml"
    app.parent.mkdir(parents=True, exist_ok=True)
    body = "" if not gate else "gate:\n" + "".join(f"  {key}: {value}\n" for key, value in gate.items())
    app.write_text(body, encoding="utf-8")
    cameras = tmp_path / "configs" / "cameras.yaml"
    camera = {"id": "gate_cam1", "name": "Gate", "role": "GATE_ENTRY", "source": "x.mp4"}
    if gate_file:
        camera["gate_file"] = gate_file
    cameras.write_text(yaml.safe_dump({"cameras": [camera]}), encoding="utf-8")
    cfg = load_config(root=tmp_path)
    return cfg, cfg.cameras[0]


# -- the shipped files -------------------------------------------------------
def test_the_shipped_gate_block_matches_the_documented_defaults():
    cfg = load_config(root=REPO_ROOT)
    assert cfg.gate.model_dump() == GateConfig().model_dump()


def test_the_shipped_gate_file_is_valid_and_has_both_roles():
    cfg = load_config(root=REPO_ROOT)
    camera = next(c for c in cfg.cameras if c.id == "gate_cam1")
    gate = gate_config_for(cfg, camera)
    assert [(line.name, line.role) for line in gate.lines] == [("in", "ENTRY"), ("out", "EXIT")]
    assert len(gate.zone) == 4
    # Timers come from app.yaml, so the gate file stays pure geometry.
    assert gate.dwell_exit_s == cfg.gate.dwell_exit_s


def test_a_lot_camera_has_no_gate_geometry_and_stays_inert():
    cfg = load_config(root=REPO_ROOT)
    camera = next(c for c in cfg.cameras if c.id == "lot_cam1")
    assert camera.gate_file is None
    gate = gate_config_for(cfg, camera)
    assert gate.lines == []
    logic = GateLogic(gate, camera.id)
    assert logic.update([], 0.0, (480, 360)) == []


# -- the merge: geometry from the file, timers from YAML ---------------------
def test_geometry_comes_from_the_gate_file_and_timers_from_app_yaml(tmp_path):
    gate_file = write_gate(tmp_path, zone=ZONE)
    cfg, camera = load(tmp_path, {"dwell_exit_s": 3.5, "min_ocr_confidence": 0.65}, gate_file)
    gate = gate_config_for(cfg, camera)
    assert gate.dwell_exit_s == 3.5
    assert gate.min_ocr_confidence == 0.65
    assert gate.track_timeout_s == GateConfig().track_timeout_s
    assert [line.name for line in gate.lines] == ["in"]
    assert gate.zone == ZONE


def test_a_camera_without_a_gate_file_gets_timers_and_no_lines(tmp_path):
    cfg, camera = load(tmp_path, {"dwell_exit_s": 4.0})
    gate = gate_config_for(cfg, camera)
    assert gate.dwell_exit_s == 4.0
    assert gate.lines == [] and gate.zone == []


def test_two_cameras_do_not_share_one_gate_files_lines(tmp_path):
    gate_file = write_gate(tmp_path)
    cfg, camera = load(tmp_path, {}, gate_file)
    first = gate_config_for(cfg, camera)
    second = gate_config_for(cfg, camera)
    first.lines[0].points[0][0] = 0.99
    assert second.lines[0].points[0][0] == pytest.approx(0.5)


# -- validation: the mistakes an operator can actually make ------------------
def test_pixel_coordinates_are_rejected_rather_than_silently_wrong():
    # A [[120, 400], [120, 900]] file looks plausible and would be wrong at any
    # other resolution, which is why gate geometry is normalised like slots.
    with pytest.raises(ValidationError, match="normalised"):
        GateLineConfig(**{**LINE, "points": [[120.0, 400.0], [120.0, 900.0]]})


def test_a_line_must_declare_whether_it_is_entry_or_exit():
    with pytest.raises(ValidationError, match="role"):
        GateLineConfig(**{k: v for k, v in LINE.items() if k != "role"})


def test_an_unknown_role_is_rejected():
    with pytest.raises(ValidationError):
        GateLineConfig(**{**LINE, "role": "MIDDLE"})


@pytest.mark.parametrize(
    "points, message",
    [
        ([[0.5, 0.1]], "exactly 2 points"),
        ([[0.5, 0.4], [0.5, 0.4]], "identical endpoints"),
    ],
)
def test_line_endpoints_are_checked(points, message):
    with pytest.raises(ValidationError, match=message):
        GateLineConfig(**{**LINE, "points": points})


def test_line_names_must_be_unique():
    with pytest.raises(ValidationError, match="unique"):
        GateConfig(lines=[LINE, dict(LINE)])


def test_the_zone_needs_a_real_polygon():
    with pytest.raises(ValidationError, match="3 or more"):
        GateConfig(zone=[[0.1, 0.1], [0.9, 0.1]])
    with pytest.raises(ValidationError, match="normalised"):
        GateConfig(zone=[[0.1, 0.1], [9.0, 0.1], [0.9, 0.7]])


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_the_review_threshold_is_a_probability(bad):
    with pytest.raises(ValidationError):
        GateConfig(min_ocr_confidence=bad)


@pytest.mark.parametrize("bad", [0.0, -2.0])
def test_the_gate_timers_are_positive(bad):
    with pytest.raises(ValidationError):
        GateConfig(dwell_exit_s=bad)
    with pytest.raises(ValidationError):
        GateConfig(track_timeout_s=bad)


def test_a_gate_file_with_no_lines_loads_as_an_empty_stage(tmp_path):
    gate_file = write_gate(tmp_path, lines=[])
    cfg, camera = load(tmp_path, {}, gate_file)
    assert gate_config_for(cfg, camera).lines == []


# -- the boundary the doc draws ----------------------------------------------
def test_gate_modules_never_read_yaml_themselves():
    """Geometry reaches GateLogic as a GateConfig, never as a file handle."""
    root = REPO_ROOT / "src" / "smart_parking"
    paths = [
        root / "parking" / "gate_logic.py",
        root / "services" / "session_service.py",
        # The Phase 6 pipeline drives both stages, so it is bound by the same rule:
        # it is handed a GateConfig, and never opens a gate file itself.
        root / "pipeline" / "gate_pipeline.py",
    ]
    offenders = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import|from)\s+yaml", text, re.MULTILINE) or "read_text" in text:
            offenders.append(path.name)
    assert offenders == []
