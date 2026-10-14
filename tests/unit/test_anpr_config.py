"""ANPR configuration chain (Phase 5, test group F).

YAML -> `load_config` -> `cfg.anpr` -> the object that actually decides. The
spec is explicit that the ANPR module may not read YAML itself, so the whole
surface area of configuration is these Pydantic models plus the loader, and
that is what this file pins down: a value typed into `configs/app.yaml` has to
reach the screen that rejects a plate, and a nonsense value has to fail at load
time rather than at 3 a.m. on a gate camera.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from smart_parking.anpr.plate_detector import PlateDetector
from smart_parking.core.config import AnprConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


class _StubDet:
    """Enough of `text_det` for the constructor's engine check."""

    def get_preprocess(self, side):
        return lambda img: img

    def infer(self, prepared):
        return [None]

    def postprocess_op(self, preds, shape):
        return None, None


class _StubOcr:
    def __init__(self):
        self.text_det = _StubDet()


def _load(tmp_path: Path, anpr: dict) -> AnprConfig:
    (tmp_path / "configs").mkdir(exist_ok=True)
    (tmp_path / "configs" / "app.yaml").write_text(
        yaml.safe_dump({"anpr": anpr}), encoding="utf-8"
    )
    return load_config(root=tmp_path, cameras_yaml=None).anpr


# -- the file reaches the object ---------------------------------------------
def test_every_anpr_subsection_is_read_from_yaml(tmp_path):
    cfg = _load(
        tmp_path,
        {
            "enabled": True,
            "detector": {"min_plate_width": 140, "max_candidates": 5},
            "ocr": {"engine": "rapidocr", "min_text_score": 0.7,
                    "preprocess": {"grayscale": False, "clahe": True}},
            "validation": {"max_repair_edits": 2, "strip_words": ["INDIA"]},
            "voting": {"window_size": 9, "minimum_observations": 4},
        },
    )
    assert cfg.detector.min_plate_width == 140
    assert cfg.detector.max_candidates == 5
    assert cfg.ocr.min_text_score == 0.7
    assert cfg.ocr.preprocess.grayscale is False
    assert cfg.ocr.preprocess.clahe is True
    assert cfg.validation.max_repair_edits == 2
    assert cfg.voting.window_size == 9
    assert cfg.voting.minimum_observations == 4


def test_configured_plate_width_changes_the_detector_decision(tmp_path):
    """Loader -> config -> the screen that actually keeps or drops a plate."""
    box = (0.0, 0.0, 120.0, 60.0)

    strict = _load(tmp_path, {"detector": {"min_plate_width": 130}})
    lenient = _load(tmp_path, {"detector": {"min_plate_width": 100}})

    assert PlateDetector(strict.detector, _StubOcr()).is_plate_shaped(box) is False
    assert PlateDetector(lenient.detector, _StubOcr()).is_plate_shaped(box) is True


def test_shipped_config_matches_the_documented_defaults():
    """`configs/app.yaml` is the tuned file; drift from the code defaults means
    one of the two was edited without telling the other."""
    from smart_parking.core.config import AppConfig

    loaded = load_config(root=REPO_ROOT, cameras_yaml=None).anpr
    assert loaded.model_dump() == AppConfig().anpr.model_dump()


def test_shipped_config_is_valid_yaml_with_an_anpr_block():
    data = yaml.safe_load((REPO_ROOT / "configs" / "app.yaml").read_text(encoding="utf-8"))
    assert "anpr" in data
    AnprConfig(**data["anpr"])  # raises if the file and the models disagree


# -- defaults and failure modes ----------------------------------------------
def test_missing_anpr_section_leaves_the_stage_enabled(tmp_path):
    """An absent block must not silently switch ANPR off the way a missing
    `false` would switch it on."""
    (tmp_path / "configs").mkdir(exist_ok=True)
    (tmp_path / "configs" / "app.yaml").write_text(
        "detection:\n  confidence: 0.4\n", encoding="utf-8"
    )
    cfg = load_config(root=tmp_path, cameras_yaml=None).anpr
    assert cfg.enabled is True
    assert cfg.ocr.enabled is True
    assert cfg.voting.minimum_observations >= 2


def test_partial_section_keeps_defaults_for_the_rest(tmp_path):
    cfg = _load(tmp_path, {"detector": {"min_plate_width": 200}})
    assert cfg.detector.min_plate_width == 200
    assert cfg.detector.max_plate_width == AnprConfig().detector.max_plate_width
    assert cfg.voting.window_size == AnprConfig().voting.window_size


@pytest.mark.parametrize(
    "anpr",
    [
        {"detector": {"min_plate_width": 0}},
        {"detector": {"max_plate_width": 50, "min_plate_width": 100}},
        {"detector": {"min_aspect_ratio": 8.0, "max_aspect_ratio": 2.0}},
        {"detector": {"det_limit_type": "both"}},
        {"ocr": {"min_text_score": 1.5}},
        {"validation": {"max_repair_edits": 9}},
        {"validation": {"min_length": 12, "max_length": 6}},
        {"voting": {"window_size": 2, "minimum_observations": 5}},
        {"voting": {"minimum_agreement": 0.0}},
        {"voting": {"max_track_age_s": 0}},
    ],
)
def test_nonsense_values_are_rejected_at_load_time(tmp_path, anpr):
    with pytest.raises(ValidationError):
        _load(tmp_path, anpr)


def test_anpr_modules_never_read_yaml_themselves():
    """The stage receives configuration through the config object only."""
    source_root = REPO_ROOT / "src" / "smart_parking" / "anpr"
    offenders = []
    for module in sorted(source_root.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        if re.search(r"^\s*(import|from)\s+yaml", text, re.MULTILINE) or "read_text" in text:
            offenders.append(module.name)
    assert offenders == []
