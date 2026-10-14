"""YOLO vehicle detection wrapper.

Pretrained COCO weights are used as-is: car/motorcycle/bus/truck are already
strong, and the doc's guidance is to fine-tune only after measuring a specific
failure. This module returns plain domain objects so nothing above it needs to
know about Ultralytics types.
"""

from __future__ import annotations

from pathlib import Path

import torch
from ultralytics import YOLO

from smart_parking.core.config import DetectionConfig
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import VEHICLE_CLASS_IDS, Detection

log = get_logger("detection.vehicle")

# Weight filenames Ultralytics will fetch itself when they are missing. Anything
# else is a file the operator placed, and a missing one is a misconfiguration,
# not a download waiting to happen.
DOWNLOADABLE_WEIGHTS = {"yolo11n.pt", "yolo11s.pt", "yolo11m.pt", "yolov8n.pt", "yolov8s.pt"}


class ModelUnavailable(RuntimeError):
    """The detection weights are missing or unreadable, with the path to fix.

    Distinct from a bare RuntimeError because a deployment has to tell "the
    model file is not where the config says" apart from every other way a
    detector can fail to start - and the message already names the remedy.
    """


def load_model(path: str | Path) -> YOLO:
    """Load detection weights, or refuse with a message that names the path.

    A missing custom file fails before Ultralytics is asked, because its own
    error for an absent local path reads like a network problem. A missing
    downloadable name is left to Ultralytics (it fetches COCO weights), but a
    failed fetch - no network, read-only directory - is re-raised as
    `ModelUnavailable` so the operator sees the path and the fix, not a stack.
    """
    p = Path(path)
    if not p.exists() and p.name not in DOWNLOADABLE_WEIGHTS:
        raise ModelUnavailable(
            f"detection weights not found at {p}; copy the .pt file there or point "
            f"detection.model_path in configs/app.yaml at the file you have"
        )
    try:
        return YOLO(str(p))
    except Exception as exc:
        raise ModelUnavailable(
            f"could not load detection weights at {p} ({exc}); copy yolo11s.pt there "
            f"or set detection.model_path in configs/app.yaml"
        ) from exc


def resolve_device(requested: str = "cuda") -> str:
    if requested.startswith("cuda") and not torch.cuda.is_available():
        log.warning("CUDA requested but unavailable - falling back to CPU")
        return "cpu"
    return requested


def precision_kwargs(fp16: bool) -> dict:
    """FP16 as Ultralytics expects it.

    8.4 renamed the `half` argument to `quantize=16`; the old name still works
    but logs a deprecation warning on *every* inference call, which buries real
    output when running a clip.
    """
    return {"quantize": 16} if fp16 else {}


class VehicleDetector:
    def __init__(self, cfg: DetectionConfig, model_path: str | None = None) -> None:
        self.cfg = cfg
        self.device = resolve_device(cfg.device)
        # The caller passes an anchored path (`AppConfig.detection_model_path`);
        # without one, the config value is used as-is, which is what a unit test
        # handing over a temporary file wants.
        self.model_path = model_path or cfg.model_path
        self.model = load_model(self.model_path)
        # Ultralytics only enables FP16 on CUDA; asking for it on CPU errors out.
        self.half = bool(cfg.half and self.device.startswith("cuda"))
        log.info(
            "loaded %s on %s (half=%s, conf=%.2f, imgsz=%d)",
            self.model_path,
            self.device,
            self.half,
            cfg.confidence,
            cfg.imgsz,
        )

    def detect(self, frame) -> list[Detection]:
        results = self.model.predict(
            frame,
            conf=self.cfg.confidence,
            iou=0.5,
            imgsz=self.cfg.imgsz,
            classes=list(self.cfg.classes),
            device=self.device,
            verbose=False,
            **precision_kwargs(self.half),
        )
        out: list[Detection] = []
        for res in results:
            boxes = res.boxes
            if boxes is None:
                continue
            for box in boxes:
                cls_id = int(box.cls[0])
                x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
                out.append(
                    Detection(
                        bbox=(x1, y1, x2, y2),
                        confidence=float(box.conf[0]),
                        class_id=cls_id,
                        class_name=VEHICLE_CLASS_IDS.get(cls_id, f"class_{cls_id}"),
                    )
                )
        return out
