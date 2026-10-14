"""Phase 1: vehicle detection on any source, with an annotated feed.

    python scripts/run_detect.py --source data/samples/parking_lot.mp4
    python scripts/run_detect.py --source 0 --show
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from smart_parking.core.config import load_config  # noqa: E402
from smart_parking.core.logging import get_logger  # noqa: E402
from smart_parking.detection.vehicle_detector import VehicleDetector  # noqa: E402
from smart_parking.video.grabber import FrameGrabber  # noqa: E402

log = get_logger("run_detect")

BOX_COLOR = (0, 60, 255)


def annotate(frame, dets, fps: float, source: str) -> "cv2.typing.CV_2DMat":
    out = frame.copy()
    for d in dets:
        x1, y1, x2, y2 = (int(v) for v in d.bbox)
        cv2.rectangle(out, (x1, y1), (x2, y2), BOX_COLOR, 2)
        label = f"{d.class_name} {d.confidence:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(out, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, max(0, y1)), BOX_COLOR, -1)
        cv2.putText(out, label, (x1 + 2, max(0, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.rectangle(out, (0, 0), (out.shape[1], 34), (30, 25, 20), -1)
    cv2.putText(
        out,
        f"{fps:5.1f} FPS   vehicles={len(dets)}   {source}",
        (10, 23),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (0, 220, 255),
        1,
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="data/samples/parking_lot.mp4", help="file path, webcam index or rtsp url")
    ap.add_argument("--show", action="store_true", help="open an OpenCV window")
    ap.add_argument("--save-video", default=None, help="write annotated mp4 here")
    ap.add_argument("--save-frames", default=None, help="dump one annotated frame per second here")
    ap.add_argument("--max-frames", type=int, default=200)
    ap.add_argument("--fps-limit", type=float, default=0.0, help="cap source read rate (0 = native)")
    ap.add_argument("--conf", type=float, default=None)
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    if args.conf is not None:
        cfg.detection.confidence = args.conf

    detector = VehicleDetector(cfg.detection, model_path=str(cfg.detection_model_path()))
    grabber = FrameGrabber(source=args.source, camera_id="detect", fps_limit=args.fps_limit).start()

    out_path = Path(args.save_video) if args.save_video else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = Path(args.save_frames) if args.save_frames else None
    if frames_dir:
        frames_dir.mkdir(parents=True, exist_ok=True)

    writer = None
    seq = -1
    processed = 0
    t0 = time.time()
    t_mark, n_mark = time.time(), 0
    t_warm: float | None = None
    fps = 0.0

    try:
        while processed < args.max_frames:
            frame, seq_now = grabber.latest(seq)
            if frame is None:
                if grabber.is_stale() and time.time() - t0 > 5:
                    log.error("no frames from %s", args.source)
                    return 1
                time.sleep(0.005)
                continue
            seq = seq_now

            dets = detector.detect(frame)
            processed += 1
            if t_warm is None:
                t_warm = time.time()
            elapsed = time.time() - t_mark
            if elapsed >= 1.0:
                fps = (processed - n_mark) / elapsed
                t_mark, n_mark = time.time(), processed

            if args.show or out_path or frames_dir:
                annotated = annotate(frame, dets, fps, args.source)
                if out_path:
                    if writer is None:
                        h, w = annotated.shape[:2]
                        writer = cv2.VideoWriter(
                            str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (w, h)
                        )
                    writer.write(annotated)
                if frames_dir and processed % 30 == 0:
                    cv2.imwrite(str(frames_dir / f"detect_{processed:05d}.jpg"), annotated)
                if args.show:
                    cv2.imshow("Phase 1 - vehicle detection", annotated)
                    if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                        break
    finally:
        grabber.stop()
        if writer:
            writer.release()
            log.info("wrote %s", out_path)
        if args.show:
            cv2.destroyAllWindows()

    duration = time.time() - t0
    warm = max(t_warm - t0, 0.0) if t_warm else 0.0
    steady = max(time.time() - t_warm, 1e-6) if t_warm else 1e-6
    print(
        f"\nframes={processed}  steady_fps={processed / steady:.1f}  "
        f"mean_fps={processed / max(duration, 1e-6):.1f} (includes {warm:.1f}s model warmup)  "
        f"device={detector.device} model={cfg.detection.model_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
