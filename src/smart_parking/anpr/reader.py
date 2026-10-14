"""Plate reading: crop, condition, recognise, validate (Phase 5).

Turns one `PlateDetection` into one `PlateObservation`. A single observation is
never an answer - it is a ballot for the voter in `anpr.voting`, and a rejected
read is recorded as a ballot for nothing rather than dropped, so the caller can
see how often a track actually produced text.
"""

from __future__ import annotations

import cv2
import numpy as np

from smart_parking.anpr.preprocess import prepare
from smart_parking.anpr.validation import PlateValidator
from smart_parking.core.config import AnprOcrConfig
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import PlateDetection, PlateObservation

log = get_logger("anpr.reader")


class PlateReader:
    def __init__(self, cfg: AnprOcrConfig, ocr, validator: PlateValidator) -> None:
        rec = getattr(ocr, "text_rec", None)
        if rec is None or not hasattr(rec, "__call__"):
            raise RuntimeError("the OCR engine does not expose text_rec()")
        self.cfg = cfg
        self._rec = rec
        self.validator = validator

    def read(
        self, frame: np.ndarray, detection: PlateDetection, condition: bool = True
    ) -> PlateObservation:
        crop = crop_plate(frame, detection.bbox)
        return self.read_crop(crop, detection, condition=condition)

    def read_crop(
        self, crop: np.ndarray, detection: PlateDetection, condition: bool = True
    ) -> PlateObservation:
        """One OCR attempt on one plate crop.

        `condition=False` skips preprocessing - the control case
        `scripts/bench_anpr.py` measures every switch against.
        """
        if crop.size == 0 or not self.cfg.enabled:
            return self._observation(detection, "", 0.0, None, 0.0)

        text, score = self.recognise(crop, condition=condition)
        if score < self.cfg.min_text_score:
            # Below the floor the engine is not confident about any of it;
            # keep the raw text for the trace, but let the vote stand as nothing.
            log.debug("low OCR score %.2f for %r", score, text)
            return self._observation(detection, text, score, None, 0.0)

        outcome = self.validator.validate(text)
        return self._observation(detection, text, score, outcome.plate, outcome.edits)

    def recognise(self, crop: np.ndarray, *, condition: bool = True) -> tuple[str, float]:
        """Raw engine call. `condition=False` is the unprocessed control case."""
        image = prepare(crop, self.cfg.preprocess) if condition else crop
        results, _elapsed = self._rec([image])
        if not results:
            return "", 0.0
        # One crop in, one region expected; the engine still answers with a list.
        text, score = results[0][0], float(results[0][1])
        return text, score

    def _observation(
        self,
        detection: PlateDetection,
        text: str,
        score: float,
        plate: str | None,
        edits: int,
    ) -> PlateObservation:
        return PlateObservation(
            track_id=detection.track_id,
            raw_text=text,
            plate=plate,
            ocr_confidence=score,
            detector_confidence=detection.confidence,
            timestamp=detection.timestamp,
            bbox=detection.bbox,
            repaired=edits > 0,
        )


def crop_plate(
    frame: np.ndarray,
    bbox: tuple[float, float, float, float],
    margin: float = 0.06,
) -> np.ndarray:
    """Clip a plate box out of a frame with a small border.

    Recognition crops text hard against the box edge and drops the first and
    last character; a few pixels of context on each side is what turns MH20AB123
    into MH20AB1234.
    """
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    mx = (x2 - x1) * margin
    my = (y2 - y1) * margin
    a = max(0, int(np.floor(x1 - mx)))
    b = max(0, int(np.floor(y1 - my)))
    c = min(width, int(np.ceil(x2 + mx)))
    d = min(height, int(np.ceil(y2 + my)))
    if c <= a or d <= b:
        return np.empty((0, 0, 3), np.uint8)
    crop = frame[b:d, a:c]
    if crop.ndim == 2:
        crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
    return crop
