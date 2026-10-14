"""Measure ANPR accuracy and latency on the labelled fixture (Phase 5).

    python scripts/bench_anpr.py                 # the whole sweep, ~10 min
    python scripts/bench_anpr.py --limit 60      # quick pass, fewer stills

Four questions, answered by measurement rather than preference:

1. OCR against plate pixel width. The exact plate rectangle from the manifest
   goes straight to the recogniser, so this isolates "can OCR read a plate this
   small" from "did the detector find it".
2. Detector recall, with the geometric screens relaxed. A miss is then readable
   as either "the text detector never fired" or "the screen threw the region
   away" - the difference between a model problem and a config problem.
3. OCR against the width the DETECTOR reported. `anpr.detector.min_plate_width`
   is applied to the proposed box and not the true one, so this is the curve the
   screen has to be set from.
4. Preprocessing. Every switch runs against the unprocessed control with its
   latency, so an operation that buys 1% for 40 ms is visibly a bad trade.

Input is `data/fixtures/plates/` (see scripts/make_plate_fixture.py): rendered
plates, not a real gate. These numbers characterise the software, not a
deployment, and none of them may be quoted as field accuracy. What a camera
delivers is the other script's job - see scripts/measure_plates.py.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
from rapidfuzz.distance import Levenshtein  # noqa: E402

from smart_parking.anpr.engine import build_ocr  # noqa: E402
from smart_parking.anpr.plate_detector import PlateDetector  # noqa: E402
from smart_parking.anpr.reader import PlateReader, crop_plate  # noqa: E402
from smart_parking.anpr.validation import PlateValidator  # noqa: E402
from smart_parking.core.config import load_config  # noqa: E402
from smart_parking.core.schemas import PlateDetection, Track  # noqa: E402

# (label, preprocess overrides against configs/app.yaml, preprocess at all?)
# The control bypasses `prepare()` entirely; every other row changes one thing
# against it, so a gain is attributable to that operation and not to the sum.
VARIANTS: list[tuple[str, dict, bool]] = [
    ("raw (control)", {}, False),
    ("grayscale", {}, True),
    ("+ clahe", {"clahe": True}, True),
    ("+ upscale", {"min_crop_height": 32}, True),
    ("upscale + clahe", {"min_crop_height": 32, "clahe": True}, True),
    ("+ sharpen", {"sharpen": True}, True),
    ("+ denoise", {"denoise": True}, True),
    ("all on", {"clahe": True, "denoise": True, "sharpen": True, "min_crop_height": 32}, True),
]


def track_from_box(bbox, track_id: int = 1) -> Track:
    x1, y1, x2, y2 = (float(v) for v in bbox)
    return Track(
        bbox=(x1, y1, x2, y2),
        confidence=0.9,
        class_id=2,
        class_name="car",
        track_id=track_id,
    )


def detection_from_rect(rect, track: Track) -> PlateDetection:
    """A known-good plate box, so OCR can be scored without the detector."""
    x1, y1, x2, y2 = (float(v) for v in rect)
    return PlateDetection(
        bbox=(x1, y1, x2, y2), confidence=1.0, track_id=track.track_id, timestamp=0.0
    )


def char_accuracy(guess: str | None, truth: str) -> float:
    if not guess:
        return 0.0
    return max(0.0, 1.0 - Levenshtein.distance(guess, truth) / max(len(guess), len(truth)))


def overlap(a, b) -> float:
    """IoU of two boxes - enough to say "that region is the plate"."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


class Tally:
    """exact / char / latency, bucketed by whatever key the caller chooses."""

    def __init__(self) -> None:
        self.buckets: dict[object, list] = defaultdict(lambda: [0, 0.0, 0.0, []])

    def add(self, key, exact: bool, chars: float, ms: float) -> None:
        row = self.buckets[key]
        row[0] += 1
        row[1] += 1.0 if exact else 0.0
        row[2] += chars
        row[3].append(ms)

    def print_rows(self) -> None:
        for key in sorted(self.buckets, key=lambda k: (isinstance(k, str), k)):
            n, exact, chars, times = self.buckets[key]
            print(
                f"{str(key):>22} {exact / n:>7.0%} {chars / n:>7.0%} "
                f"{statistics.median(times):>9.1f} {n:>5}"
            )


def header(title: str) -> None:
    print(f"\n{title}")
    print(f"{'':>22} {'exact':>7} {'chars':>7} {'ms/crop':>9} {'n':>5}")
    print("-" * 55)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", default=str(ROOT / "data" / "fixtures" / "plates"))
    ap.add_argument("--limit", type=int, default=None, help="max stills")
    ap.add_argument(
        "--ablation-limit", type=int, default=140, help="stills in the ablation sweep, 0 for all"
    )
    # `det_limit_type` decides how DBNet scales a vehicle crop, and the choice in
    # `configs/app.yaml` is justified by a measured comparison, so the comparison
    # has to be re-runnable from the command line rather than trusted from a
    # comment written once.
    ap.add_argument(
        "--limit-type",
        choices=("min", "max"),
        default=None,
        help="override anpr.detector.det_limit_type for this run",
    )
    args = ap.parse_args()

    manifest_path = Path(args.fixture) / "manifest.json"
    if not manifest_path.exists():
        print(f"{manifest_path} missing - run scripts/make_plate_fixture.py first")
        return 2
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    if args.limit:
        entries = entries[: args.limit]

    cfg = load_config(root=ROOT)
    if args.limit_type:
        cfg.anpr.detector = cfg.anpr.detector.model_copy(
            update={"det_limit_type": args.limit_type}
        )
    ocr = build_ocr(cfg.anpr)
    validator = PlateValidator(cfg.anpr.validation)
    screens = cfg.anpr.detector
    reader = PlateReader(cfg.anpr.ocr, ocr, validator)
    # Two views of one engine: the real screens, and a relaxed copy for recall.
    detector = PlateDetector(screens, ocr)
    open_detector = detector.relaxed()

    scenes = []
    for entry in entries:
        image = cv2.imread(str(Path(args.fixture) / entry["file"]))
        if image is not None:
            scenes.append((entry, image))
    if not scenes:
        print("no fixture images readable")
        return 2

    # Warm the engine before any timing claim: the first call builds the ONNX
    # sessions and costs seconds on its own.
    warm_start = time.perf_counter()
    reader.recognise(crop_plate(scenes[0][1], scenes[0][0]["plate_rect"]))
    print(
        f"SYNTHETIC FIXTURE - rendered plates, not photographed. {len(scenes)} stills. "
        f"Engine warm-up {time.perf_counter() - warm_start:.1f}s, excluded from the "
        "latency figures below."
    )

    header("1) OCR on the EXACT plate box, by true plate width")
    by_true = Tally()
    by_degradation = Tally()
    crops = []
    for entry, image in scenes:
        det = detection_from_rect(entry["plate_rect"], track_from_box(entry["car_box"]))
        started = time.perf_counter()
        observation = reader.read(image, det)
        ms = (time.perf_counter() - started) * 1000
        truth = entry["ground_truth"]
        exact = observation.plate == truth
        chars = char_accuracy(observation.plate, truth)
        by_true.add(entry["plate_width_px"], exact, chars, ms)
        by_degradation.add(entry["degradation"], exact, chars, ms)
        crops.append((entry, crop_plate(image, entry["plate_rect"]), det))
    by_true.print_rows()

    header("   the same crops, split by how they were degraded")
    by_degradation.print_rows()

    print("\n2) plate DETECTOR on the vehicle box (screens relaxed)")
    located = screened = 0
    region_widths: list[float] = []
    proposals: list[tuple] = []
    detect_times: list[float] = []
    for entry, image in scenes:
        started = time.perf_counter()
        regions = open_detector.detect(image, track_from_box(entry["car_box"]), 0.0)
        detect_times.append((time.perf_counter() - started) * 1000)
        region_widths.extend(r.width for r in regions)
        best = max(regions, key=lambda r: overlap(r.bbox, entry["plate_rect"]), default=None)
        if best is not None and overlap(best.bbox, entry["plate_rect"]) >= 0.3:
            located += 1
            if detector.is_plate_shaped(best.bbox):
                screened += 1
                proposals.append((entry, image, best))
    total = len(scenes)
    print(f"  plate region located (IoU >= 0.3): {located}/{total} = {located / total:.0%}")
    print(f"  of those, the real screens keep:   {screened}/{located} = {screened / max(1, located):.0%}")
    if region_widths:
        region_widths.sort()
        print(
            f"  every region proposed: min {region_widths[0]:.0f} "
            f"median {region_widths[len(region_widths) // 2]:.0f} "
            f"max {region_widths[-1]:.0f} px  ({len(region_widths) / total:.1f} per vehicle)"
        )
    print(f"  detector latency: median {statistics.median(detect_times):.1f} ms/vehicle")
    print(
        f"  screens in effect: width {screens.min_plate_width:.0f}-{screens.max_plate_width:.0f}px, "
        f"aspect {screens.min_aspect_ratio}-{screens.max_aspect_ratio}, "
        f"vehicle >= {screens.min_vehicle_width:.0f}px, "
        f"roi_bottom_fraction {screens.roi_bottom_fraction}, pad {screens.crop_pad_px}px"
    )

    header("3) OCR on the DETECTOR's box, by PROPOSED width")
    print("   (min_plate_width screens on the proposed box, not the true one)")
    by_proposed = Tally()
    for entry, image, detection in proposals:
        started = time.perf_counter()
        observation = reader.read(image, detection)
        ms = (time.perf_counter() - started) * 1000
        truth = entry["ground_truth"]
        proposed = detection.bbox[2] - detection.bbox[0]
        by_proposed.add(
            round(proposed / 25) * 25,
            observation.plate == truth,
            char_accuracy(observation.plate, truth),
            ms,
        )
    by_proposed.print_rows()

    header("4) preprocessing ablation on the exact crop (gain against cost)")
    sample = crops if not args.ablation_limit else crops[::2][: args.ablation_limit]
    for label, overrides, condition in VARIANTS:
        pre = cfg.anpr.ocr.preprocess.model_copy(update=overrides)
        run = PlateReader(cfg.anpr.ocr.model_copy(update={"preprocess": pre}), ocr, validator)
        tally = Tally()
        for entry, crop, det in sample:
            started = time.perf_counter()
            observation = run.read_crop(crop, det, condition=condition)
            ms = (time.perf_counter() - started) * 1000
            truth = entry["ground_truth"]
            tally.add(label, observation.plate == truth, char_accuracy(observation.plate, truth), ms)
        tally.print_rows()
    print("\n  keep only the switches that pay for themselves; the defaults are the winners.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
