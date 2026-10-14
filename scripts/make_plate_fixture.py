"""Generate the ANPR test fixture (Phase 5).

    python scripts/make_plate_fixture.py

Writes `data/fixtures/plates/` (still crops with known ground truth plus a
`manifest.json`) and `data/fixtures/gate_synth.mp4` (a vehicle carrying one
plate through a short clip).

WHY THIS EXISTS: there is no entrance-camera recording on this machine, and a
plate detector cannot be built or measured without plates to point at. These
images are **rendered, not photographed**. They prove the pipeline is wired
correctly end to end - detection, crop, OCR, validation, voting, association -
and they are the only numbers here that come from synthetic input. They are
never evidence that ANPR works on a real gate, and nothing may report them as
if it were.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "fixtures" / "plates"

# Widths in pixels spanning "hopeless" to "comfortable", so the accuracy curve
# can be read off the fixture instead of guessed at.
PLATE_WIDTHS = (30, 45, 60, 80, 110, 150, 200)
PLATES = (
    "MH20AB1234",
    "TS09DP4567",
    "DL12CD8901",
    "KA01MN2345",
    "GJ18BB5678",
    "TN09AX1122",
    "23AH1234",
    "UP16AB7788",
)
_FONTS = None
_DEGRADATIONS = ("clean", "blur", "noise", "compressed", "tilt")
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_plate(plate: str, width: int) -> np.ndarray:
    """A single-plate strip, white ground, black border, dark characters."""
    from PIL import Image, ImageDraw

    height = max(6, int(round(width / 2.3)))
    img = Image.new("RGB", (width, height), (238, 236, 226))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(20, 20, 20), width=max(1, height // 22))
    text = plate
    size = max(6, int(height * 0.72))
    font = _font(size)
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad_x = max(1, int(width * 0.04))
    # Shrink the font until the string fits the strip: narrow plates at small
    # widths would otherwise clip the last character.
    while tw > width - 2 * pad_x and size > 5:
        size -= 1
        font = _font(size)
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(((width - tw) / 2 - box[0], (height - th) / 2 - box[1]), text, font=font, fill=(15, 15, 20))
    return np.asarray(img)[:, :, ::-1].copy()


def render_scene(
    plate_image: np.ndarray, seed: int, degrade: str = "clean", jitter: bool = True
) -> tuple[np.ndarray, tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Glue a plate onto a drawn car rear at gate-camera scale.

    The car is crude on purpose - it is background for the plate, not the
    subject. Degradation variants are what make the accuracy numbers mean
    anything: a pristine render flatters every pipeline.

    Returns the scene, the car box and the exact plate rectangle. YOLO does not
    detect a drawn car, so the vehicle stage cannot be tested here; the boxes go
    into the manifest so the plate stages can be driven directly - and the
    plate rect separately, which is what lets the bench tell detector misses
    apart from OCR failures.
    """
    rng = np.random.default_rng(seed)
    canvas = np.full((360, 480, 3), (95, 105, 115), np.uint8)
    # road + kerb
    cv2.rectangle(canvas, (0, 250), (480, 360), (60, 62, 66), -1)
    cv2.rectangle(canvas, (0, 248), (480, 252), (200, 200, 200), -1)

    h, w = plate_image.shape[:2]
    body_w, body_h = int(w * 3.4), int(h * 9.0)
    dx = int(rng.integers(-12, 12)) if jitter else 0
    dy = int(rng.integers(-8, 8)) if jitter else 0
    cx, cy = 240 + dx, 210 + dy
    x1, y1 = max(0, cx - body_w // 2), max(0, cy - body_h // 2)
    x2, y2 = min(480, x1 + body_w), min(360, y1 + body_h)
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (35, 45, 130), -1)
    cv2.rectangle(canvas, (x1 + 6, y1 + 8), (x2 - 6, y1 + body_h // 3), (15, 20, 30), -1)
    light = (25, 30, 200)
    cv2.rectangle(canvas, (x1 + 4, y2 - 26), (x1 + 30, y2 - 6), light, -1)
    cv2.rectangle(canvas, (x2 - 30, y2 - 26), (x2 - 4, y2 - 6), light, -1)
    cv2.rectangle(canvas, (x1, y2 - 6), (x2, y2), (20, 20, 22), -1)

    px, py = cx - w // 2, y2 - h - 12
    px = max(0, min(480 - w, px))
    py = max(0, min(360 - h, py))
    canvas[py : py + h, px : px + w] = plate_image

    if degrade == "blur":
        k = 5 if w < 80 else 7
        canvas = cv2.GaussianBlur(canvas, (k, k), 0)
    elif degrade == "noise":
        noise = rng.normal(0, 9.0, canvas.shape)
        canvas = np.clip(canvas.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    elif degrade == "compressed":
        ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 35])
        canvas = cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else canvas
    elif degrade == "tilt":
        canvas = _tilt(canvas, (px, py, w, h), rng)

    # For the tilted variant the warp moves the plate by a few pixels; the
    # recorded rect stays the pre-warp one and is approximate there.
    return canvas, (x1, y1, x2, y2), (px, py, px + w, py + h)


def _tilt(image: np.ndarray, plate_rect, rng) -> np.ndarray:
    """A mild perspective so the plate is not perfectly frontal."""
    px, py, w, h = plate_rect
    src = np.float32([[px, py], [px + w, py], [px + w, py + h], [px, py + h]])
    jitter = float(rng.uniform(0.06, 0.14))
    dst = np.float32(
        [
            [px + w * 0.04, py + h * jitter],
            [px + w * 0.96, py - h * jitter],
            [px + w * 0.98, py + h * 1.02],
            [px + w * 0.02, py + h * 0.98],
        ]
    )
    out = cv2.warpPerspective(
        image, cv2.getPerspectiveTransform(src, dst), (image.shape[1], image.shape[0]),
        borderMode=cv2.BORDER_REPLICATE,
    )
    return out


def make_stills() -> list[dict]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    index = 0
    for width in PLATE_WIDTHS:
        for plate in PLATES:
            for degrade in _DEGRADATIONS:
                plate_img = render_plate(plate, width)
                scene, car, rect = render_scene(plate_img, seed=index, degrade=degrade)
                name = f"w{width:03d}_{plate}_{degrade}.png"
                cv2.imwrite(str(OUT_DIR / name), scene)
                manifest.append(
                    {
                        "file": name,
                        "ground_truth": plate,
                        "plate_width_px": width,
                        "car_box": list(car),
                        "plate_rect": list(rect),
                        "degradation": degrade,
                        "scene_size": [scene.shape[1], scene.shape[0]],
                    }
                )
                index += 1
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def make_video(frames: int, fps: int) -> tuple[Path, Path]:
    """One vehicle, one plate, approaching the camera - the voting test bed.

    The car box is written beside the clip so a run can feed the same real
    tracker output shape (`Track`) into the plate stages without YOLO having to
    recognise a drawn car, which it does not.
    """
    plate_text = "MH20AB1234"
    path = ROOT / "data" / "fixtures" / "gate_synth.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (480, 360))
    if not writer.isOpened():
        raise RuntimeError(f"could not open a video writer for {path}")

    boxes: list[dict] = []
    for i in range(frames):
        # Approaches the camera: the plate grows, then holds still long enough
        # for the vote to settle. No jitter, so the recorded box is exact.
        t = min(1.0, i / max(1, frames - 1))
        width = int(70 + 130 * t) if t < 0.7 else 200
        plate_img = render_plate(plate_text, width)
        scene, car, rect = render_scene(
            plate_img, seed=1000 + i, degrade="noise" if i % 3 else "blur", jitter=False
        )
        writer.write(scene)
        boxes.append(
            {
                "frame": i,
                "track_id": 1,
                "bbox": list(car),
                "plate_rect": list(rect),
                "ground_truth": plate_text,
            }
        )
    writer.release()

    meta = path.with_name("gate_synth_tracks.json")
    meta.write_text(
        json.dumps({"fps": fps, "frames": frames, "tracks": boxes}, indent=2), encoding="utf-8"
    )
    return path, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video-frames", type=int, default=40)
    ap.add_argument("--fps", type=int, default=15)
    args = ap.parse_args()

    manifest = make_stills()
    print(f"{len(manifest)} stills -> {OUT_DIR}")
    video, meta = make_video(args.video_frames, args.fps)
    print(f"clip ({args.video_frames} frames) -> {video}")
    print(f"car boxes -> {meta}")
    print("SYNTHETIC FIXTURE - rendered plates, not photographed. Do not quote as field accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
