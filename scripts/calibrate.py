"""Phase 2: draw slot polygons on a camera snapshot and save normalised JSON.

    python scripts/calibrate.py --source data/samples/parking_lot.mp4 --out configs/slots/lot_a_cam1.json
    python scripts/calibrate.py --source data/samples/parking_lot.mp4 --grid   # no mouse: dump a coordinate grid

Phase 6 adds the gate equivalent - entry/exit lines and a dwell zone:

    python scripts/calibrate.py --gate --source data/samples/gate_traffic.mp4 \
            --camera-id gate_cam1 --out configs/gates/gate_cam1.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from smart_parking.core.logging import get_logger  # noqa: E402
from smart_parking.parking.calibration import (  # noqa: E402
    draw_grid,
    grab_snapshot,
    interactive,
    interactive_gate,
)

log = get_logger("calibrate")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="data/samples/parking_lot.mp4")
    ap.add_argument("--out", default=None, help="default: configs/slots/... or configs/gates/...")
    ap.add_argument("--camera-id", default=None)
    ap.add_argument("--gate", action="store_true", help="draw entry/exit lines and a dwell zone")
    ap.add_argument("--grid", action="store_true", help="write a gridded snapshot instead of opening the mouse tool")
    ap.add_argument("--step", type=int, default=50, help="grid spacing in pixels")
    args = ap.parse_args()

    source = str(ROOT / args.source) if not Path(args.source).is_absolute() else args.source
    frame = grab_snapshot(source)
    log.info("snapshot %dx%d from %s", frame.shape[1], frame.shape[0], args.source)

    if args.grid:
        out = Path(ROOT / (args.out or "configs/slots/lot_a_cam1.json")).with_suffix(".jpg")
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), draw_grid(frame, args.step))
        log.info("wrote gridded snapshot -> %s", out)
        return 0

    if args.gate:
        out = Path(ROOT / (args.out or "configs/gates/gate_a.json"))
        camera_id = args.camera_id or out.stem
        n = interactive_gate(source, out, camera_id)
        log.info("gate calibration finished with %d lines", n)
        return 0 if n else 1

    out = Path(ROOT / (args.out or "configs/slots/lot_a_cam1.json"))
    n = interactive(source, out, args.camera_id or "lot_cam1")
    log.info("calibration finished with %d slots", n)
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
