"""Plate-crop conditioning before recognition (Phase 5).

Every operation here is a config switch that was measured, not assumed - see
`scripts/bench_anpr.py`, which runs the variants against each other and prints
per-variant accuracy. The defaults are only the ones that improved the score
without costing latency.

Nothing in this module knows about plates as *text*: it takes a crop in pixels
and hands a crop back in pixels.
"""

from __future__ import annotations

import cv2
import numpy as np

from smart_parking.core.config import AnprPreprocessConfig

_KERNEL = np.array([[0.0, -0.25, 0.0], [-0.25, 2.0, -0.25], [0.0, -0.25, 0.0]], np.float32)


def prepare(crop: np.ndarray, cfg: AnprPreprocessConfig) -> np.ndarray:
    """Apply the enabled operations, in the order that keeps them comparable.

    Resize first: contrast and sharpening behave differently on a 14-pixel-tall
    strip than on the same strip enlarged to 48, and the OCR engine resizes to
    its own height afterwards regardless.
    """
    if crop is None or crop.size == 0:
        return crop

    image = crop
    if cfg.min_crop_height and image.shape[0] < cfg.min_crop_height:
        image = _upscale(image, cfg.target_height)
    if cfg.grayscale and image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if cfg.clahe:
        image = _clahe(image)
    if cfg.denoise:
        image = _denoise(image)
    if cfg.sharpen:
        image = _sharpen(image)
    if image.ndim == 2:
        # PP-OCR expects 3 channels; converting back is a no-op for the model
        # but keeps the array shape uniform for whatever the engine does next.
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return image


def _upscale(image: np.ndarray, target_height: int) -> np.ndarray:
    scale = max(1.0, target_height / max(1, image.shape[0]))
    if scale <= 1.0:
        return image
    size = (int(round(image.shape[1] * scale)), int(round(image.shape[0] * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_CUBIC)


def _clahe(image: np.ndarray) -> np.ndarray:
    source = image
    if source.ndim == 3:
        lab = cv2.cvtColor(source, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
        return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(source)


def _denoise(image: np.ndarray) -> np.ndarray:
    h = image.shape[0]
    strength = max(3.0, min(9.0, h / 8.0))
    if image.ndim == 2:
        return cv2.fastNlMeansDenoising(image, None, strength, 7, 21)
    return cv2.fastNlMeansDenoisingColored(image, None, strength, strength, 7, 21)


def _sharpen(image: np.ndarray) -> np.ndarray:
    return cv2.filter2D(image, -1, _KERNEL)
