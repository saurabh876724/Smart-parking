"""Frame sources: file, webcam and RTSP behind one factory.

Keeping this as a factory means the pipeline never knows whether it is being
fed a demo clip or a live camera, which is what lets us test the AI on plain
video files.
"""

from __future__ import annotations

import re
from pathlib import Path

import cv2

from smart_parking.core.logging import get_logger

log = get_logger("video.sources")

# RTSP urls look like rtsp://user:pass@host:554/axis-media/... - never log them raw.
_CRED_RE = re.compile(r"(?<=://)[^@/]+(?=@)")


def redact(source: str) -> str:
    return _CRED_RE.sub("***", source)


_URL_SCHEMES = ("rtsp://", "rtspt://", "rtmp://", "http://", "https://")


def is_webcam(source: str) -> bool:
    return source.isdigit()


def is_file_source(source: str) -> bool:
    """True for a video file, false for a device index or a stream url."""
    return not (source.isdigit() or source.startswith(_URL_SCHEMES))


def open_capture(source: str) -> cv2.VideoCapture:
    """Build a VideoCapture for a file path, device index or RTSP url."""
    if is_webcam(source):
        cap = cv2.VideoCapture(int(source), cv2.CAP_DSHOW)
    elif source.startswith(("rtsp://", "rtspt://", "rtmp://", "http://", "https://")):
        # FFMPEG backend handles RTSP over TCP far more reliably than the
        # default MSMF/DSHOW wrappers on Windows.
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"video file not found: {path}")
        cap = cv2.VideoCapture(str(path))

    if not cap.isOpened():
        raise RuntimeError(f"could not open source: {redact(source)}")
    return cap


def describe(source: str) -> dict:
    cap = open_capture(source)
    try:
        return {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": cap.get(cv2.CAP_PROP_FPS) or 0.0,
            "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        }
    finally:
        cap.release()
