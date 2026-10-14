"""Measure plate geometry on real footage, before claiming ANPR will work (Phase 5).

    python scripts/measure_plates.py                       # gate_cam1 from configs
    python scripts/measure_plates.py --stride 5 --max-frames 300
    python scripts/measure_plates.py --with-ocr            # + valid-plate rate

ANPR is a resolution problem before it is an OCR problem. Whether a plate can be
read is decided by how many pixels wide it is in the frame, and that is set by the
camera: its resolution, its distance to the plate and its angle. None of those are
in this repository's control once the camera is on a pole, so the first ANPR
question on any deployment is a measurement, not a configuration choice:

    how wide is a number plate, in pixels, in the footage we actually have?

This script answers that with the real vehicle detector and the real plate
detector, and reports percentiles rather than a mean - the tail is the part that
fails. It prints no accuracy figures, because accuracy needs ground truth and
unlabelled footage cannot supply it; the only honest number here is geometry.

`scripts/bench_anpr.py` does the opposite: it has ground truth (rendered plates)
and no real world. Read the two together - the width curve from the fixture says
what N pixels buys, this script says how many pixels the camera delivers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from smart_parking.anpr.engine import build_ocr  # noqa: E402
from smart_parking.anpr.plate_detector import PlateDetector  # noqa: E402
from smart_parking.anpr.reader import PlateReader  # noqa: E402
from smart_parking.anpr.validation import PlateValidator  # noqa: E402
from smart_parking.core.config import CameraConfig, load_config  # noqa: E402
from smart_parking.tracking.tracker import Tracker  # noqa: E402
from smart_parking.video.sources import is_file_source, open_capture  # noqa: E402

# Percentiles worth printing: the bottom of the distribution is the failure case,
# the top is what a close crop looks like.
POINTS = (0, 5, 25, 50, 75, 95, 100)
# Enough stills to eyeball whether the boxes are plates, without filling the disk.
MAX_STILLS = 40


def percentiles(values: list[float]) -> dict[int, float]:
    if not values:
        return {}
    ordered = sorted(values)
    out = {}
    for p in POINTS:
        index = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
        out[p] = ordered[index]
    return out


LABELS = {0: "min", 5: "p5", 25: "p25", 50: "median", 75: "p75", 95: "p95", 100: "max"}


def fmt(values: list[float], unit: str = "px") -> str:
    p = percentiles(values)
    if not p:
        return "no samples"
    return "  ".join(f"{LABELS[pct]}={p[pct]:.0f}{unit}" for pct in POINTS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", default="gate_cam1", help="id from configs/cameras.yaml")
    ap.add_argument("--source", default=None, help="override the camera source")
    ap.add_argument("--stride", type=int, default=3, help="sample every Nth frame")
    ap.add_argument("--max-frames", type=int, default=400, help="frames to sample")
    ap.add_argument("--with-ocr", action="store_true", help="also report the valid-plate rate")
    ap.add_argument("--save-frames", default=None, help="write annotated stills of plate candidates")
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    camera = next((c for c in cfg.cameras if c.id == args.camera), None)
    if camera is None:
        camera = CameraConfig(id=args.camera, name=args.camera, source=args.source or "")
    if args.source:
        camera.source = args.source
    if not camera.source:
        print(f"camera {camera.id} has no source")
        return 2
    source = camera.source if Path(camera.source).is_absolute() else str(ROOT / camera.source)
    if not is_file_source(source) or not Path(source).exists():
        print(f"{source} is not a readable video file - this script measures recorded footage")
        return 2

    if not cfg.anpr.enabled:
        print("anpr.enabled is false in configs/app.yaml - nothing to measure")
        return 2

    ocr = build_ocr(cfg.anpr)
    screens = cfg.anpr.detector
    detector = PlateDetector(screens, ocr)
    # Relaxed copy: proposes every text region so a miss can be read as either
    # "never detected" or "screened out". Same ONNX sessions, no extra cost.
    open_detector = detector.relaxed()
    reader = PlateReader(cfg.anpr.ocr, ocr, PlateValidator(cfg.anpr.validation)) if args.with_ocr else None

    tracker = Tracker(model=_yolo(cfg.detection.model_path), cfg=cfg.detection, enabled=True)

    cap = open_capture(source)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"source      {source}")
    print(f"resolution  {width}x{height}  fps={fps:.2f}  frames={total_frames}  stride={args.stride}")
    print(f"screens     min_plate_width={screens.min_plate_width:.0f}px "
          f"max={screens.max_plate_width:.0f}px aspect={screens.min_aspect_ratio}-{screens.max_aspect_ratio} "
          f"min_vehicle_width={screens.min_vehicle_width:.0f}px")
    print("no ground truth exists on this footage, so nothing below is an accuracy figure.\n")

    vehicle_widths: list[float] = []
    candidate_widths: list[float] = []
    widest_per_vehicle: list[float] = []
    widest_with_screen: list[float] = []
    aspects: list[float] = []
    vehicles = vehicles_with_region = vehicles_passing = 0
    frames_sampled = 0
    detect_ms = ocr_ms = 0.0
    detect_calls = ocr_calls = 0
    valid_reads = 0
    saved = 0

    seq = -1
    while frames_sampled < args.max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        seq += 1
        if stride_skip(seq, args.stride):
            continue
        frames_sampled += 1
        # Video time, not wall-clock: the same number the pipeline would use.
        now = seq / fps if fps > 0 else float(seq)
        tracks = tracker.update(frame)
        annotated = frame.copy() if args.save_frames else None
        drew_regions = False

        for track in tracks:
            vehicles += 1
            vehicle_widths.append(track.bbox[2] - track.bbox[0])
            started = cv2.getTickCount()
            regions = open_detector.detect(frame, track, now)
            detect_calls += 1
            detect_ms += (cv2.getTickCount() - started) / cv2.getTickFrequency() * 1000
            if not regions:
                continue
            vehicles_with_region += 1
            widest = max(regions, key=lambda r: r.bbox[2] - r.bbox[0])
            widest_width = widest.bbox[2] - widest.bbox[0]
            widest_height = max(1e-6, widest.bbox[3] - widest.bbox[1])
            candidate_widths.extend(r.bbox[2] - r.bbox[0] for r in regions)
            widest_per_vehicle.append(widest_width)
            aspects.append(widest_width / widest_height)
            if detector.is_plate_shaped(widest.bbox):
                vehicles_passing += 1
                widest_with_screen.append(widest_width)
                if reader is not None:
                    started = cv2.getTickCount()
                    observation = reader.read(frame, widest)
                    ocr_calls += 1
                    ocr_ms += (cv2.getTickCount() - started) / cv2.getTickFrequency() * 1000
                    if observation.accepted:
                        valid_reads += 1
            if annotated is not None:
                for r in regions:
                    x1, y1, x2, y2 = (int(v) for v in r.bbox)
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 1)
                    cv2.putText(
                        annotated,
                        f"{r.bbox[2] - r.bbox[0]:.0f}",
                        (x1, max(12, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.4,
                        (0, 255, 255),
                        1,
                    )
                    drew_regions = True

        if args.save_frames and drew_regions and saved < MAX_STILLS:
            cv2.imwrite(str(_frame_path(args.save_frames, seq)), annotated)
            saved += 1

    cap.release()

    cap.release()

    print(f"frames sampled        {frames_sampled}  vehicle detections {vehicles}")
    print(f"vehicles with >=1 text region: {vehicles_with_region}/{vehicles} "
          f"({vehicles_with_region / max(1, vehicles):.0%})")
    print(f"of those, widest region passes the screens: {vehicles_passing}/{vehicles_with_region} "
          f"({vehicles_passing / max(1, vehicles_with_region):.0%})")

    print("\nvehicle box widths        " + fmt(vehicle_widths))
    print("all text region widths    " + fmt(candidate_widths))
    print("widest region per vehicle " + fmt(widest_per_vehicle))
    print("widest region that passed " + fmt(widest_with_screen))
    print("widest region aspect      " + fmt(aspects, ""))

    # The suitability verdict, stated as the comparison the reader cares about.
    if widest_per_vehicle:
        enough = [w for w in widest_per_vehicle if w >= screens.min_plate_width]
        print(
            f"\n{len(enough)}/{len(widest_per_vehicle)} vehicle instances "
            f"({len(enough) / len(widest_per_vehicle):.0%}) offered a text region at or above the "
            f"configured min_plate_width of {screens.min_plate_width:.0f}px."
        )
        if not enough:
            print(
                "  Nothing in this footage is wide enough for the OCR stage. That is a camera "
                "verdict, not a software one: on the labelled fixture (scripts/bench_anpr.py) "
                "recognition needs roughly 150px before it reads a plate reliably, and this "
                "source never proposes a region that wide. ANPR cannot be validated here - "
                "record an entrance camera at a resolution and distance that put a plate above "
                "that floor, or raise the source resolution, and re-run this script."
            )
        elif len(enough) / len(widest_per_vehicle) < 0.25:
            print(
                "  Only a minority of vehicles are readable at this distance and resolution. "
                "The chain works where the plate clears the floor; coverage is the camera's, "
                "not the pipeline's."
            )
    if args.with_ocr:
        print(
            f"OCR returned a VALID plate for {valid_reads}/{ocr_calls} vehicles whose region "
            "passed the screens. Unverified: no ground truth, so this is a legibility "
            "signal, not accuracy."
        )
        print(f"plate detector {detect_ms / max(1, detect_calls):.1f} ms/vehicle, "
              f"OCR {ocr_ms / max(1, ocr_calls):.1f} ms/crop")
    else:
        print(f"plate detector {detect_ms / max(1, detect_calls):.1f} ms/vehicle "
              "(--with-ocr adds the recognition figures)")
    if args.save_frames:
        print(f"annotated stills in {args.save_frames} ({saved} frames) - check by eye that "
              "the boxes are plates and not signage")
    return 0


def stride_skip(seq: int, stride: int) -> bool:
    # Frame 0 is always processed so a short clip still yields samples.
    return stride > 1 and seq % stride and seq != 0


def _frame_path(directory: str, seq: int) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"plates_{seq:06d}.jpg"


def _yolo(model_path: str):
    from ultralytics import YOLO

    return YOLO(model_path)


if __name__ == "__main__":
    raise SystemExit(main())
