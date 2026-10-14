"""Threaded frame grabber that keeps only the newest frame.

Real-time systems must drop frames, not queue them. A reader thread spins on
the capture and overwrites a single slot, so a slow inference loop makes the
system stale by one frame rather than by seconds.
"""

from __future__ import annotations

import threading
import time
from enum import Enum

import cv2
import numpy as np

from smart_parking.core.logging import get_logger
from smart_parking.video.sources import is_file_source, open_capture, redact

log = get_logger("video.grabber")


class GrabberStatus(str, Enum):
    STARTING = "STARTING"
    LIVE = "LIVE"
    RECONNECTING = "RECONNECTING"
    OFFLINE = "OFFLINE"
    STOPPED = "STOPPED"


class FrameGrabber:
    """Pull frames off one source in the background.

    ``latest()`` returns (frame, seq) or (None, seq) when nothing new arrived.
    The sequence number lets callers detect repeats instead of processing the
    same frame twice.
    """

    def __init__(
        self,
        source: str,
        camera_id: str = "cam1",
        fps_limit: float = 0.0,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        stale_after_s: float = 5.0,
        loop_file: bool = True,
    ) -> None:
        self.source = source
        self.camera_id = camera_id
        self.fps_limit = fps_limit
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.stale_after_s = stale_after_s
        self.loop_file = loop_file and is_file_source(source)

        self._frame: np.ndarray | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = GrabberStatus.STARTING
        self.last_frame_ts = 0.0
        self.reconnects = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FrameGrabber":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"grab-{self.camera_id}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.status = GrabberStatus.STOPPED

    def __enter__(self) -> "FrameGrabber":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- consumer API ------------------------------------------------------
    def latest(self, since_seq: int = -1) -> tuple[np.ndarray | None, int]:
        with self._lock:
            if self._seq == since_seq:
                return None, self._seq
            return self._frame, self._seq

    def is_stale(self) -> bool:
        if self.last_frame_ts == 0.0:
            return True
        return (time.time() - self.last_frame_ts) > self.stale_after_s

    # -- producer thread ---------------------------------------------------
    def _run(self) -> None:
        delay = 1.0 / self.fps_limit if self.fps_limit > 0 else 0.0
        backoff = self.reconnect_min_s

        while not self._stop.is_set():
            cap = None
            try:
                cap = open_capture(self.source)
                backoff = self.reconnect_min_s
                self.status = GrabberStatus.LIVE
                ret, frame = cap.read()
                if not ret and self.loop_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()
                if not ret:
                    raise RuntimeError("first read failed")

                while not self._stop.is_set():
                    ret, frame = cap.read()
                    if not ret:
                        if self.loop_file:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            continue
                        if is_file_source(self.source):
                            # A finished clip is not a fault: stop instead of
                            # reconnecting forever.
                            log.info("%s: end of clip after %d frames", self.camera_id, self._seq)
                            self.status = GrabberStatus.STOPPED
                            return
                        raise RuntimeError("end of stream or read error")

                    with self._lock:
                        self._frame = frame
                        self._seq += 1
                    self.last_frame_ts = time.time()

                    if delay:
                        time.sleep(delay)

            except Exception as err:  # reconnect with exponential backoff
                if self._stop.is_set():
                    break
                self.status = GrabberStatus.RECONNECTING
                self.reconnects += 1
                log.warning(
                    "%s: %s - retrying in %.1fs (%d so far)",
                    self.camera_id,
                    err,
                    backoff,
                    self.reconnects,
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.reconnect_max_s)
            finally:
                if cap is not None:
                    cap.release()

        self.status = GrabberStatus.STOPPED
        log.info("%s: grabber stopped", self.camera_id)
