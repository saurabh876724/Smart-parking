"""Run ANPR over one camera source (Phase 5).

    python scripts/run_anpr.py --camera gate_cam1 --stride 5        # real footage
    python scripts/run_anpr.py --tracks data/fixtures/gate_synth_tracks.json \
                              --source data/fixtures/gate_synth.mp4  # labelled replay

The pipeline is the same either way: vehicle tracks in, plate candidates out,
OCR per candidate, one validated string per frame, a per-track vote, and a single
`plate_read` event when a plate becomes stable. What changes is where the tracks
come from.

`--tracks` replays a labelled box file instead of running YOLO. It exists for one
reason: a rendered or otherwise synthetic clip has no real cars in it, so the
vehicle detector finds nothing, and without vehicle boxes the ANPR stages cannot
be exercised at all. When it is used the script says so on every line of output
that would otherwise look like a field result.

Occupancy is not touched. This reads plates and writes events; slots, FSMs and
sessions belong to the phases that already own them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from smart_parking.anpr.engine import build_ocr  # noqa: E402
from smart_parking.anpr.pipeline import AnprPipeline  # noqa: E402
from smart_parking.core.config import CameraConfig, load_config  # noqa: E402
from smart_parking.core.events import RecordingEventBus  # noqa: E402
from smart_parking.core.schemas import PLATE_READ  # noqa: E402
from smart_parking.tracking.replay import tracks_by_frame  # noqa: E402
from smart_parking.tracking.tracker import Tracker  # noqa: E402
from smart_parking.video.sources import open_capture  # noqa: E402


def draw(frame: np.ndarray, current: list, hud: str) -> np.ndarray:
    """Vehicle boxes, the plate each track is thought to carry, and a line of stats.

    Green is a stable plate, amber a leader that has not been confirmed yet - the
    distinction the whole voting layer exists to make visible.
    """
    out = frame.copy()
    for item in current:
        track = item[0]
        x1, y1, x2, y2 = (int(v) for v in track.bbox)
        cv2.rectangle(out, (x1, y1), (x2, y2), (255, 200, 0), 1)
        result = item[1]
        if result is None:
            continue
        bx1, by1, bx2, by2 = (int(v) for v in result.bbox)
        colour = (0, 255, 0) if result.stable else (0, 160, 255)
        cv2.rectangle(out, (bx1, by1), (bx2, by2), colour, 2)
        cv2.putText(
            out,
            f"{result.plate_text}{' *' if result.stable else ''} {result.observations} reads",
            (bx1, max(16, by1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            colour,
            1,
        )
    cv2.putText(out, hud, (8, out.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", default="gate_cam1", help="id from configs/cameras.yaml")
    ap.add_argument("--source", default=None, help="override the camera source")
    ap.add_argument("--tracks", default=None, help="labelled box file: replay instead of YOLO")
    ap.add_argument("--stride", type=int, default=None, help="process every Nth frame")
    ap.add_argument("--max-frames", type=int, default=None, help="frames to process")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--save-video", default=None)
    ap.add_argument("--quiet", action="store_true", help="no per-frame plate lines")
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    camera = next((c for c in cfg.cameras if c.id == args.camera), None)
    if camera is None:
        camera = CameraConfig(id=args.camera, name=args.camera, source=args.source or "")
    if args.source:
        camera.source = args.source
    if not camera.source:
        print(f"camera {camera.id} has no source - set one in configs/cameras.yaml or pass --source")
        return 2
    source = camera.source if Path(camera.source).is_absolute() else str(ROOT / camera.source)
    if not Path(source).exists():
        print(f"{source} does not exist")
        return 2

    if not cfg.anpr.enabled:
        print("anpr.enabled is false in configs/app.yaml")
        return 2

    rows = None
    if args.tracks:
        path = Path(args.tracks)
        if not path.is_absolute():
            path = ROOT / path
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload["tracks"] if isinstance(payload, dict) else payload
        truth = {int(r["track_id"]): r["ground_truth"] for r in rows if r.get("ground_truth")}
        first_stable: dict[int, float] = {}
        replay = tracks_by_frame(rows, cfg.occupancy.footprint_ratio)
    else:
        truth, first_stable, replay = {}, {}, None

    ocr = build_ocr(cfg.anpr)
    bus = RecordingEventBus()
    pipeline = AnprPipeline(cfg, camera.id, bus=bus, ocr=ocr)
    tracker = (
        None
        if rows is not None
        else Tracker(model=_yolo(cfg.detection.model_path), cfg=cfg.detection, enabled=True)
    )

    cap = open_capture(source)
    fps = cap.get(cv2.CAP_PROP_FPS) or (rows and rows[0].get("fps")) or 15.0
    stride = args.stride or cfg.detection.frame_stride
    writer = None

    print(
        f"camera={camera.id}  source={source}\n"
        f"tracks={'labelled replay from ' + str(args.tracks) if rows is not None else 'YOLO + ByteTrack'}"
        f"  stride={stride}\n"
        f"ocr={cfg.anpr.ocr.engine}  min_plate_width={cfg.anpr.detector.min_plate_width:.0f}px"
        f"  vote={cfg.anpr.voting.minimum_observations}/{cfg.anpr.voting.window_size} reads"
        f" at >= {cfg.anpr.voting.minimum_agreement:.0%} agreement\n"
        + "-" * 72
    )
    if rows is not None:
        print("LABELLED REPLAY: vehicle boxes were supplied, not detected. "
              "Plate results below are only as real as the footage.")

    seq = -1
    processed = 0
    latest: dict[int, object] = {}  # last plate per track, kept past window expiry
    t_start = time.perf_counter()
    t_warm: float | None = None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            seq += 1
            if stride > 1 and seq % stride:
                continue
            if args.max_frames and processed >= args.max_frames:
                break
            # Video time, exactly as the lot pipeline times its FSMs: a replay
            # running 3x faster than the camera must not look 3x quicker to the
            # voting window.
            now = seq / fps
            tracks = replay.get(seq, []) if replay is not None else tracker.update(frame)
            results = pipeline.process(frame, tracks, now)
            processed += 1
            if t_warm is None:
                t_warm = time.perf_counter()

            for result in results:
                first_stable.setdefault(result.track_id, now)
                latest[result.track_id] = result
                if not args.quiet:
                    print(
                        f"  frame {seq:>5} t={now:6.2f}s  track {result.track_id:>3} -> "
                        f"{result.plate_text}  ({result.observations} reads, "
                        f"{result.agreement:.0%} agreement, ocr {result.ocr_confidence:.2f})"
                    )
            # The overlay reports what the pipeline currently believes about each
            # track, stable or not - `plate_for` is the operator-facing view.
            beliefs = [(track, pipeline.plate_for(track.track_id)) for track in tracks]
            if args.show or args.save_video:
                hud = (
                    f"{camera.id}  frame {seq}  vehicles {pipeline.stats.vehicles} "
                    f"reads {pipeline.stats.reads} stable {pipeline.stats.stable_results}"
                )
                annotated = draw(frame, beliefs, hud)
                if args.save_video:
                    writer = writer or _make_writer(args.save_video, annotated.shape)
                    writer.write(annotated)
                if args.show:
                    cv2.imshow("Smart Parking - ANPR", annotated)
                    if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                        break
    finally:
        cap.release()
        if writer is not None:
            writer.release()
            print(f"wrote {args.save_video}")
        if args.show:
            cv2.destroyAllWindows()

    elapsed = time.perf_counter() - t_start
    steady = max(time.perf_counter() - (t_warm or t_start), 1e-6)
    stats = pipeline.stats
    plate_events = [e for e in bus.recorded if e.type == PLATE_READ]

    print("-" * 72)
    print(
        f"frames processed {processed}  vehicles seen {stats.vehicles}  "
        f"plate candidates {stats.candidates}  OCR reads {stats.reads}"
    )
    print(
        f"accepted reads     {stats.accepted_reads} ({stats.read_accept_rate:.0%} of reads)  "
        f"of which repaired {stats.repaired_reads}"
    )
    print(f"stable plates     {stats.stable_results}  events published {len(plate_events)}")
    print(
        f"latency           detector {stats.detect_seconds / max(1, stats.vehicles) * 1000:.1f} ms/vehicle, "
        f"OCR {stats.ocr_seconds / max(1, stats.reads) * 1000:.1f} ms/read"
    )
    print(
        f"throughput        {processed / elapsed:.1f} frames/s overall "
        f"(includes {(t_warm or t_start) - t_start:.1f}s engine warm-up), "
        f"{(processed - 1) / steady if processed > 1 else 0:.1f} frames/s steady state"
    )

    if truth:
        print("\ntrack  predicted        ground truth     stable at  verdict")
        print("-" * 66)
        correct = wrong = unread = 0
        for track_id, expected in sorted(truth.items()):
            result = latest.get(track_id)
            when = first_stable.get(track_id)
            if result is None:
                verdict, counts = "NO PLATE READ", ""
                unread += 1
            elif result.plate_text == expected:
                verdict, counts = "correct", ""
                correct += 1
            else:
                verdict, counts = f"WRONG ({result.plate_text} != {expected})", ""
                wrong += 1
            print(
                f"{track_id:>5}  {(result.plate_text if result else '-'):<16} {expected:<16} "
                f"{(f'{when:.2f}s' if when is not None else 'never'):<10} {verdict}{counts}"
            )
        print(f"\n{correct} correct, {wrong} wrong, {unread} never read of {len(truth)} tracks")
        print("Synthetic fixture: this validates the software chain, not a gate.")
    return 0


def _yolo(model_path: str):
    from ultralytics import YOLO

    return YOLO(model_path)


def _make_writer(path: str | Path, shape) -> cv2.VideoWriter:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    h, w = shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(p), fourcc, 12.0, (w, h))
    if not writer.isOpened():
        print(f"mp4v writer refused {p} - falling back to .avi")
        writer = cv2.VideoWriter(str(p.with_suffix(".avi")), cv2.VideoWriter_fourcc(*"MJPG"), 12.0, (w, h))
    return writer


if __name__ == "__main__":
    raise SystemExit(main())
