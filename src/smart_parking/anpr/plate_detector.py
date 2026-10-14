"""Plate candidate detection (Phase 5).

Generic vehicle detection is not a plate detector: a YOLO box around a car says
nothing about where the registration number is. This module takes one vehicle
box and proposes the small text regions inside it that could be a plate, using
three independent screens - detector score, pixel size, and shape.

Geometry proposes, text disposes: a shop sign behind a car passes every
geometric screen here and is eliminated later, by validation. That separation
is deliberate - the plate *format* is not a shape property, and folding it in
here would mean running OCR on everything to decide what to run OCR on.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import cv2
import numpy as np

from smart_parking.core.config import AnprDetectorConfig
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import PlateDetection, Track

log = get_logger("anpr.detector")

# rapidocr-onnxruntime 1.4.x exposes det as `.text_det` with `.get_preprocess`,
# `.infer` and `.postprocess_op`. The adapter below depends on exactly those.
_SUPPORTED_ENGINE = "rapidocr-onnxruntime>=1.4,<2"


@dataclass(frozen=True)
class TextRegion:
    """One text region in the coordinates of the image it was run on."""

    bbox: tuple[float, float, float, float]
    score: float

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


class PlateDetector:
    def __init__(self, cfg: AnprDetectorConfig, ocr) -> None:
        self.cfg = cfg
        det = getattr(ocr, "text_det", None)
        for name in ("get_preprocess", "infer", "postprocess_op"):
            if det is None or not hasattr(det, name):
                raise RuntimeError(
                    f"the OCR engine does not expose text_det.{name}() - "
                    f"expected {_SUPPORTED_ENGINE}"
                )
        self._det = det

    def relaxed(self) -> "PlateDetector":
        """A copy that proposes *every* text region instead of only plate-shaped ones.

        Measurement needs the two failure modes kept apart: if the screens are
        applied while counting recall, a screen that is simply too tight looks
        identical to a detector that never fired. Scripts measure with this copy
        and then apply the real `is_plate_shaped` to the regions it found.

        A shallow copy shares the ONNX sessions; only the screens differ.
        """
        twin = copy.copy(self)
        twin.cfg = self.cfg.model_copy(
            update={
                "min_plate_width": 1.0,
                "max_plate_width": 1e6,
                "min_aspect_ratio": 0.2,
                "max_aspect_ratio": 40.0,
                "min_vehicle_width": 0.0,
                "roi_bottom_fraction": 1.0,
                "max_candidates": 50,
            }
        )
        return twin

    def detect(self, frame: np.ndarray, track: Track, timestamp: float) -> list[PlateDetection]:
        """Plate candidates for one vehicle, in frame coordinates."""
        if track.bbox[2] - track.bbox[0] < self.cfg.min_vehicle_width:
            return []

        x1, y1, x2, y2 = self._roi(track.bbox, frame.shape)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return []

        pad = self.cfg.crop_pad_px
        regions = self.text_regions(cv2.copyMakeBorder(roi, pad, pad, pad, pad, cv2.BORDER_REPLICATE))
        out: list[PlateDetection] = []
        for region in regions:
            # Undo the padding, then the ROI offset, to land back in frame space.
            bbox = (
                region.bbox[0] - pad + x1,
                region.bbox[1] - pad + y1,
                region.bbox[2] - pad + x1,
                region.bbox[3] - pad + y1,
            )
            if self.is_plate_shaped(bbox):
                out.append(
                    PlateDetection(
                        bbox=bbox,
                        confidence=region.score,
                        track_id=track.track_id,
                        timestamp=timestamp,
                    )
                )
        # Widest first: on a rear view the plate is the largest text on the car.
        out.sort(key=lambda d: -(d.bbox[2] - d.bbox[0]))
        return out[: self.cfg.max_candidates]

    def is_plate_shaped(self, bbox: tuple[float, float, float, float]) -> bool:
        """The configured geometric screen, exposed so a measurement can apply
        it to regions found by a `relaxed()` detector."""
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if height <= 0:
            return False
        if not self.cfg.min_plate_width <= width <= self.cfg.max_plate_width:
            return False
        ratio = width / height
        return self.cfg.min_aspect_ratio <= ratio <= self.cfg.max_aspect_ratio

    def text_regions(self, image: np.ndarray) -> list[TextRegion]:
        """Raw text regions with per-box scores.

        The engine's own `__call__` throws the box scores away and returns only
        polygons, and it filters by a minimum size that is far too generous for
        plates. Both are needed here, so the detector stages are driven directly
        and only the geometric screens below are applied.
        """
        height, width = image.shape[:2]
        prepared = self._det.get_preprocess(max(height, width))(image)
        prediction = self._det.infer(prepared)[0]
        boxes, scores = self._det.postprocess_op(prediction, (height, width))
        if boxes is None or len(boxes) == 0:
            return []

        regions: list[TextRegion] = []
        for box, score in zip(boxes, np.asarray(scores).ravel()):
            xs, ys = box[:, 0], box[:, 1]
            regions.append(
                TextRegion(
                    bbox=(float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())),
                    score=float(score),
                )
            )
        return regions

    def _roi(self, bbox: tuple[float, float, float, float], shape: tuple[int, ...]):
        """The bottom slice of a vehicle box, where number plates live."""
        height, width = shape[:2]
        x1, y1, x2, y2 = (int(round(v)) for v in bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        top = y2 - int(round((y2 - y1) * self.cfg.roi_bottom_fraction))
        return x1, max(y1, top), x2, y2
