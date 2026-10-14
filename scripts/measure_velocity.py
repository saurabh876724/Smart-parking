"""Measure track speeds on a clip so the velocity threshold can be set per camera.

`max_parking_velocity` is in pixels/second at source resolution, so it is not
portable between cameras: it depends on mounting height, lens, resolution and
how far down the frame the bays are. Run this against a recording of the actual
camera and pick a threshold between the two populations it reports - vehicles
that end up parked in a bay, and vehicles that only ever drive past.

    python scripts/measure_velocity.py                      # camera from configs
    python scripts/measure_velocity.py --camera lot_cam1 --window 15
    python scripts/measure_velocity.py --trace 1            # frame-by-frame trace
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from smart_parking.core.config import load_config, load_slots  # noqa: E402
from smart_parking.parking.geometry import (  # noqa: E402
    containment_score,
    slots_from_config,
    to_pixels,
)
from smart_parking.tracking.tracker import Tracker  # noqa: E402
from smart_parking.tracking.velocity import VelocityGate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", default="lot_cam1")
    ap.add_argument("--window", type=int, default=None, help="override smoothing_window")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--trace", type=int, default=None, metavar="TRACK_ID",
                    help="print one track's speed and containment per frame")
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    camera = next((c for c in cfg.cameras if c.id == args.camera), None)
    if camera is None:
        print(f"no camera '{args.camera}' in configs/cameras.yaml")
        return 2

    source = str(ROOT / camera.source) if not Path(camera.source).is_absolute() else camera.source
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"cannot open {source}")
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    slots = {}
    if camera.slots_file:
        slots = slots_from_config(load_slots(ROOT / camera.slots_file, ROOT), camera.id)
        slots = {sid: to_pixels(poly, width, height) for sid, poly in slots.items()}

    gate_cfg = cfg.velocity_gating
    if args.window:
        gate_cfg = gate_cfg.model_copy(update={"smoothing_window": args.window})
    tracker = Tracker(YOLO(cfg.detection.model_path), cfg.detection, enabled=True)
    gate = VelocityGate(gate_cfg)

    speeds: dict[int, list[float]] = defaultdict(list)
    best: dict[int, tuple[float, str]] = defaultdict(lambda: (0.0, "-"))
    seen: dict[int, int] = defaultdict(int)

    print(f"\n{camera.id}  {Path(source).name}  {width}x{height} @ {fps:.0f}fps   "
          f"window={gate_cfg.smoothing_window} frames ({gate_cfg.smoothing_window / fps:.2f}s)")
    if args.trace is not None:
        print(f"\n{'frame':>6} {'t(s)':>6} {'px/s':>8} {'contain':>8} {'slot':>5}")

    index = 0
    while True:
        ok, frame = cap.read()
        if not ok or (args.max_frames and index >= args.max_frames):
            break
        # Video time, exactly as the pipeline stamps it during replay.
        now = index / fps
        tracks = tracker.update(frame)
        gate.update(tracks, now)
        for tr in tracks:
            seen[tr.track_id] += 1
            speed = gate.speed(tr.track_id)
            if speed is not None:
                speeds[tr.track_id].append(speed)
            fits = [(containment_score(tr.footprint, poly), sid) for sid, poly in slots.items()]
            frame_fit, frame_slot = max(fits) if fits else (0.0, "-")
            if frame_fit > best[tr.track_id][0]:
                best[tr.track_id] = (frame_fit, frame_slot)
            if args.trace is not None and tr.track_id == args.trace:
                txt = "None" if speed is None else f"{speed:.1f}"
                print(f"{index:>6} {now:>6.2f} {txt:>8} {frame_fit:>8.2f} {frame_slot:>5}")
        index += 1
    cap.release()

    if args.trace is not None:
        return 0

    print(f"\n{'track':>6} {'frames':>7} {'med':>7} {'p90':>7} {'max':>8} {'contain':>8} slot")
    for track_id in sorted(speeds, key=lambda t: -seen[t]):
        values = sorted(speeds[track_id])
        med = values[len(values) // 2]
        p90 = values[int(len(values) * 0.9)] if len(values) > 1 else values[0]
        fit, slot_id = best[track_id]
        print(f"{track_id:>6} {seen[track_id]:>7} {med:>7.1f} {p90:>7.1f} {values[-1]:>8.1f} "
              f"{fit:>8.2f} {slot_id:>5}")

    # Separate the two populations by the widest multiplicative gap in their
    # median speeds: parked vehicles cluster near zero, moving ones do not.
    medians = sorted(
        (sorted(speeds[t])[len(speeds[t]) // 2], t) for t in speeds if speeds[t]
    )
    print("\nmedian px/s by track: "
          + ", ".join(f"{m:.1f} (t{t})" for m, t in medians))

    gaps = [
        (hi / lo, lo, hi, lo_id, hi_id)
        for (lo, lo_id), (hi, hi_id) in zip(medians, medians[1:])
        if lo > 0
    ]
    if not gaps:
        print("not enough distinct tracks to suggest a threshold")
        return 0

    ratio, lo, hi, lo_id, hi_id = max(gaps)
    suggest = math.sqrt(hi * lo)
    print(f"widest gap: t{lo_id} at {lo:.1f} -> t{hi_id} at {hi:.1f} px/s ({ratio:.1f}x)")
    print(f"suggested max_parking_velocity: ~{suggest:.0f} px/s "
          f"(geometric midpoint of that gap; configured {gate_cfg.max_parking_velocity})")
    print("Sanity-check it against the p90 column: a track you know is parked must "
          "stay under the threshold most of the time, or its bay will never fill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
