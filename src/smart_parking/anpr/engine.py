"""OCR engine construction (Phase 5).

One place touches the third-party API, so the rest of the ANPR layer takes an
opaque engine object and can be unit tested with a stub.

The engine is PP-OCRv4 through `rapidocr-onnxruntime`. Why that and not the
alternatives is measured, not preferred - see the README Phase 5 section:
EasyOCR pulls `opencv-python-headless` 5.0, which would overwrite the `cv2`
4.13 the occupancy pipeline runs on, and PaddleOCR needs a second deep-learning
runtime next to the CUDA torch stack. RapidOCR's ONNX models ship inside the
wheel, so there is no weight download at run time and no new CUDA provider.
"""

from __future__ import annotations

from smart_parking.core.config import AnprConfig
from smart_parking.core.logging import get_logger

log = get_logger("anpr.engine")


def build_ocr(cfg: AnprConfig):
    """Construct the text detector + recogniser described by `cfg`.

    The returned object exposes `.text_det` and `.text_rec`; callers must not
    depend on anything else from this library, which is what keeps the upgrade
    surface in this file.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise RuntimeError(
            "ANPR needs the OCR engine: install the anpr extra "
            "(`pip install -e .[anpr]` or `pip install rapidocr-onnxruntime`)"
        ) from exc

    ocr = RapidOCR(
        print_verbose=False,
        # Recognition score floor: below this the text is noise, and a noisy
        # read must not be allowed to vote.
        text_score=cfg.ocr.min_text_score,
        det_limit_side_len=cfg.detector.det_side_len,
        # Plate crops are upright; the angle classifier only costs latency.
        no_cls=not cfg.ocr.use_orientation_cls,
    )
    # `limit_type` is the only scaling knob the constructor does not accept, and
    # it decides whether `det_side_len` means anything at all: get_preprocess()
    # ignores the side length unless the mode is "min". Measured on the fixture,
    # "max" located 88% of plates at 264 ms/vehicle against 60% at 625 ms for
    # "min", because upscaling a small crop blurs the strokes DBNet learned on.
    ocr.text_det.limit_type = cfg.detector.det_limit_type
    log.info(
        "OCR engine ready (det_limit_type=%s, det_side_len=%d, min_text_score=%.2f, cls=%s)",
        cfg.detector.det_limit_type,
        cfg.detector.det_side_len,
        cfg.ocr.min_text_score,
        cfg.ocr.use_orientation_cls,
    )
    return ocr
