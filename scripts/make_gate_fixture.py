"""Generate the Phase 6 gate fixture: vehicles with known crossing times.

    python scripts/make_gate_fixture.py

Writes `data/fixtures/gate_visit.mp4` (480x360, 15 fps, 6 s) and
`data/fixtures/gate_visit_tracks.json`, which holds the per-frame vehicle boxes
(so a run can replay them instead of running YOLO) *and* the ground truth the
run is scored against: the frame each vehicle passes each gate line, and which
vehicles must produce nothing at all.

WHY THIS EXISTS: there is no gate-camera recording on this machine, so entry/exit
correctness has to be proven against an input whose answer is known. Like the
Phase 5 fixture, everything here is rendered, not photographed. It is evidence
that crossings, sessions, dwell exits, dedupe and plate binding are wired
correctly end to end; it is never evidence about a real barrier.

The ground truth is computed from the boxes that are actually written to the
file - a vehicle sliding out of frame has its box clipped, and a clipped box has
a different centroid, so the expectation follows what the gate stage really
sees rather than what the path says on paper.

Five cases, the five Phase 6 behaviours worth proving:

  1  a vehicle that crosses in and later crosses back out  -> one visit, one duration
  2  a vehicle that wobbles at the barrier as it crosses   -> exactly one entry
  3  a vehicle that drives out of frame instead of crossing the exit line -> dwell exit
  4  a vehicle that only ever crosses the exit line        -> unmatched exit, an anomaly
  5  a vehicle travelling outside the gate zone            -> no events at all
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_plate_fixture import render_plate  # noqa: E402

WIDTH, HEIGHT, FPS, FRAMES = 480, 360, 15, 90
LINE_X = 0.5 * WIDTH
ZONE_NORMALISED = [[0.10, 0.10], [0.90, 0.10], [0.90, 0.75], [0.10, 0.75]]
# The fixture zone is an axis-aligned rectangle, so its bounding box is the
# zone; min/max over every point rather than indexing two of them.
ZONE = (
    min(p[0] for p in ZONE_NORMALISED) * WIDTH,
    min(p[1] for p in ZONE_NORMALISED) * HEIGHT,
    max(p[0] for p in ZONE_NORMALISED) * WIDTH,
    max(p[1] for p in ZONE_NORMALISED) * HEIGHT,
)
PLATE_WIDTH = 140


def _ramp(start_x, speed, first_frame, last_frame, hold_at=None, wobble=0.0):
    """A linear x path, optionally held after `last_frame` and wobbling +/- px."""
    def path(f: int) -> float | None:
        if f < first_frame or f > FRAMES - 1:
            return None
        steps = min(f, last_frame) - first_frame
        x = start_x + speed * steps
        if hold_at is not None and f > last_frame:
            x = hold_at
        if wobble:
            x += wobble if f % 2 else -wobble
        return x

    return path


VEHICLES = (
    {
        "track_id": 1,
        "plate": "MH20AB1234",
        "colour": (35, 45, 130),
        "y": 198,
        "body": (220, 100),
        "case": "crosses in, waits, crosses back out",
        "expect": "one entry, one exit by line, duration ~2.9s",
        "path": _ramp(58, 12.0, 0, 30),
        "path_back": _ramp(418, -12.0, 45, 75, hold_at=58),
    },
    {
        "track_id": 2,
        "plate": "TS09DP4567",
        "colour": (130, 45, 35),
        "y": 112,
        "body": (200, 92),
        "case": "wobbles +/-5 px as it crosses the barrier",
        "expect": "exactly one entry, no exit - it stays in view",
        "path": _ramp(152, 8.0, 10, 41, hold_at=400, wobble=5.0),
    },
    {
        "track_id": 3,
        "plate": "DL12CD8901",
        "colour": (40, 120, 40),
        "y": 246,
        "body": (200, 92),
        "case": "crosses in, then drives out of frame on the right",
        "expect": "entry by line, exit by zone dwell",
        # Runs to the end of the clip: a ramp that froze at its last_frame would
        # leave the car parked at the frame edge, still sighted forever, and the
        # dwell timer could never close it.
        "path": _ramp(122, 8.3, 0, FRAMES - 1),
    },
    {
        "track_id": 4,
        "plate": "KA01MN2345",
        "colour": (120, 40, 120),
        "y": 168,
        "body": (200, 92),
        "case": "only ever crosses right-to-left",
        "expect": "exit with no entry: anomaly, no duration",
        "path": _ramp(400, -7.9, 55, 89),
    },
    {
        "track_id": 5,
        "plate": "GJ18BB5678",
        "colour": (60, 60, 60),
        "y": 20,
        "body": (120, 40),
        "case": "runs along the top edge, outside the gate zone",
        "expect": "no events at all",
        "path": _ramp(32, 5.0, 0, 89),
    },
)


def x_at(vehicle, f: int) -> float | None:
    """The vehicle's centre x at frame f, forward path first, then the return."""
    back = vehicle.get("path_back")
    if back is not None and f >= 45:
        return back(f)
    return vehicle["path"](f)


def road() -> np.ndarray:
    canvas = np.full((HEIGHT, WIDTH, 3), (95, 105, 115), np.uint8)
    cv2.rectangle(canvas, (0, 250), (WIDTH, HEIGHT), (60, 62, 66), -1)
    cv2.rectangle(canvas, (0, 248), (WIDTH, 252), (200, 200, 200), -1)
    return canvas


def paste_vehicle(canvas, plate_text: str, x: float, y: int, body, colour):
    """Draw one vehicle rear with its plate on it; return its clipped box."""
    plate_img = render_plate(plate_text, PLATE_WIDTH)
    ph, pw = plate_img.shape[:2]
    bw, bh = body
    x1, y1 = max(0, int(round(x - bw / 2))), max(0, y - bh // 2)
    x2, y2 = min(WIDTH, x1 + bw), min(HEIGHT, y1 + bh)
    cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, -1)
    cv2.rectangle(canvas, (x1 + 6, y1 + 6), (x2 - 6, y1 + bh // 3), (15, 20, 30), -1)
    for sx in (x1 + 4, x2 - 34):
        cv2.rectangle(canvas, (sx, y2 - 22), (sx + 30, y2 - 6), (25, 30, 200), -1)

    px = max(0, min(WIDTH - pw, int(round((x1 + x2) / 2 - pw / 2))))
    py = max(0, min(HEIGHT - ph, y2 - ph - 8))
    canvas[py : py + ph, px : px + pw] = plate_img
    return x1, y1, x2, y2


def in_zone(cx: float, cy: float) -> bool:
    return ZONE[0] <= cx <= ZONE[2] and ZONE[1] <= cy <= ZONE[3]


def expected(frames: list[int], xs: list[float], ys: list[float]) -> dict:
    """Crossings, read off the emitted box centroids.

    A crossing is a sign change of (x - LINE_X) between consecutive sightings,
    and - as in the implementation - a step whose midpoint lies outside the gate
    zone is not the barrier. Left-to-right is the ENTRY direction of the fixture
    gate file, right-to-left the EXIT.
    """
    entry = exit_ = None
    crossings = []
    for i in range(1, len(xs)):
        prev, cur = xs[i - 1], xs[i]
        if (prev - LINE_X) * (cur - LINE_X) >= 0:
            continue
        mid_x, mid_y = (prev + cur) / 2.0, (ys[i - 1] + ys[i]) / 2.0
        if not in_zone(mid_x, mid_y):
            continue
        direction = "ltr" if cur > prev else "rtl"
        crossings.append({"frame": frames[i], "s": round(frames[i] / FPS, 3), "direction": direction})
        if direction == "ltr" and entry is None:
            entry = frames[i]
        if direction == "rtl" and exit_ is None:
            exit_ = frames[i]
    in_zone_frames = [f for f, cx, cy in zip(frames, xs, ys) if in_zone(cx, cy)]
    still_in_zone = bool(in_zone_frames) and in_zone_frames[-1] == frames[-1]
    return {
        "crossings": crossings,
        "expected_entry_frame": entry,
        "expected_exit_frame": exit_,
        # No exit line, and it left the gate area (or the frame) afterwards: the
        # dwell timer has to close it. A vehicle still sitting in the zone when
        # the clip ends is an open visit, not an exit.
        "expected_dwell_frame": (
            in_zone_frames[-1]
            if entry is not None and exit_ is None and in_zone_frames and not still_in_zone
            else None
        ),
        "last_seen_frame": frames[-1] if frames else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/fixtures/gate_visit.mp4")
    args = ap.parse_args()

    video = ROOT / args.out
    video.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise RuntimeError(f"could not open a video writer for {video}")

    sightings: dict[int, dict] = {v["track_id"]: {"frames": [], "xs": [], "ys": []} for v in VEHICLES}
    rows: list[dict] = []
    for f in range(FRAMES):
        canvas = road()
        for vehicle in VEHICLES:
            x = x_at(vehicle, f)
            if x is None:
                continue
            bw = vehicle["body"][0]
            if x - bw / 2 >= WIDTH or x + bw / 2 <= 0:
                # Whole body off the tarmac: the tracker has nothing left to
                # report, which is what a vehicle leaving the camera's view looks
                # like downstream. The clipped box that `paste_vehicle` returns
                # would say otherwise - it never leaves the frame.
                continue
            box = paste_vehicle(canvas, vehicle["plate"], x, vehicle["y"], vehicle["body"], vehicle["colour"])
            cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
            rows.append({"frame": f, "track_id": vehicle["track_id"], "bbox": list(box)})
            seen = sightings[vehicle["track_id"]]
            seen["frames"].append(f)
            seen["xs"].append(cx)
            seen["ys"].append(cy)
        writer.write(canvas)
    writer.release()

    expectations = []
    for vehicle in VEHICLES:
        seen = sightings[vehicle["track_id"]]
        answer = expected(seen["frames"], seen["xs"], seen["ys"])
        answer.update(
            {
                "track_id": vehicle["track_id"],
                "ground_truth": vehicle["plate"],
                "case": vehicle["case"],
                "expect": vehicle["expect"],
                "sightings": len(seen["frames"]),
            }
        )
        expectations.append(answer)

    meta = video.with_name("gate_visit_tracks.json")
    meta.write_text(
        json.dumps(
            {
                "fps": FPS,
                "frames": FRAMES,
                "size": [WIDTH, HEIGHT],
                "synthetic": True,
                "gate_file": "configs/gates/gate_visit_synth.json",
                "note": "Rendered fixture. Boxes are given, not detected; plates are drawn, not photographed.",
                "expectations": expectations,
                "tracks": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"clip ({FRAMES} frames @ {FPS} fps) -> {video}")
    print(f"boxes + ground truth ({len(rows)} rows) -> {meta}")
    for answer in expectations:
        print(
            f"  track {answer['track_id']:>2}  {answer['case']}\n"
            f"       entry={answer['expected_entry_frame']} exit={answer['expected_exit_frame']}"
            f" dwell={answer['expected_dwell_frame']} last_seen={answer['last_seen_frame']}"
            f"  -> {answer['expect']}"
        )
    print("SYNTHETIC FIXTURE - proves the software chain, not a gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
