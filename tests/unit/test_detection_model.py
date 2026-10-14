"""The detection weights: where they live, and what happens when they do not.

`detection.model_path` is a project-relative path like every other path in this
project, and Ultralytics must never be handed a bare name: it would resolve it
against the *working directory*, and - failing that - download a second copy
of the weights into it. A server started from anywhere but the project root
would otherwise miss the file or fetch it again, and a deployment whose image
lacks the weights deserves an error that names the path and the fix, not an
HTTP stack trace from inside a vendor library.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smart_parking.core.config import AppConfig, DetectionConfig, load_config
from smart_parking.detection.vehicle_detector import ModelUnavailable, load_model

ROOT = Path(__file__).resolve().parents[2]


def test_the_model_path_is_anchored_to_the_project_root(tmp_path):
    cfg = AppConfig(
        project_root=tmp_path, detection=DetectionConfig(model_path="models/yolo11s.pt")
    )
    assert cfg.detection_model_path() == tmp_path / "models" / "yolo11s.pt"
    # An absolute path is already anchored: resolve() leaves it alone.
    absolute = (tmp_path / "elsewhere.pt").as_posix()
    cfg.detection.model_path = absolute
    assert cfg.detection_model_path() == Path(absolute)


def test_the_shipped_config_points_at_a_file_the_repository_carries():
    """The weights travel with the project (models/), so a fresh clone and a
    fresh image agree on where detection looks."""
    shipped = load_config(root=ROOT, cameras_yaml=None)
    assert shipped.detection.model_path == "models/yolo11s.pt"
    assert shipped.detection_model_path() == ROOT / "models" / "yolo11s.pt"
    assert shipped.detection_model_path().exists()


def test_a_missing_custom_weight_fails_before_any_download_is_attempted(tmp_path):
    missing = tmp_path / "my_finetune.pt"
    with pytest.raises(ModelUnavailable) as caught:
        load_model(missing)
    message = str(caught.value)
    assert str(missing) in message
    assert "detection.model_path" in message


def test_a_failed_load_of_a_downloadable_name_names_the_path_and_the_fix(tmp_path, monkeypatch):
    """A COCO name that is missing is Ultralytics' cue to fetch it; when the
    fetch dies (no network, read-only home) the operator still gets the path."""
    import smart_parking.detection.vehicle_detector as vd

    def refuse(path):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(vd, "YOLO", refuse)
    with pytest.raises(ModelUnavailable) as caught:
        load_model(tmp_path / "yolo11s.pt")
    assert str(tmp_path / "yolo11s.pt") in str(caught.value)
