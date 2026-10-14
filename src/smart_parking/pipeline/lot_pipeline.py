"""LOT camera loop: frames -> vehicles -> slots -> events.

    frame -> ROI/tracks -> slot assignment (overlap) -> slot FSMs -> events

Runs headless (writing frames or a video) or with a window, which is what makes
the same code usable in CI and in a demo.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import cv2

from smart_parking.core.config import AppConfig, CameraConfig
from smart_parking.core.events import EventBus, InMemoryEventBus
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import FrameView, SlotState, SlotStatus
from smart_parking.parking.occupancy_engine import OccupancyEngine
from smart_parking.parking.render import draw_hud, draw_slots, draw_tracks
from smart_parking.tracking.tracker import Tracker
from smart_parking.tracking.velocity import VelocityGate
from smart_parking.video.grabber import FrameGrabber, GrabberStatus
from smart_parking.video.sources import is_file_source, open_capture

log = get_logger("pipeline.lot")

# A camera that has not delivered a frame for this long is considered down.
OFFLINE_AFTER_S = 5.0


class LotPipeline:
    def __init__(
        self,
        cfg: AppConfig,
        camera: CameraConfig,
        slots: dict,
        bus: EventBus | None = None,
        track: bool = True,
        loop: bool = False,
    ) -> None:
        self.cfg = cfg
        self.camera = camera
        self.slots = slots
        # `bus or ...` would discard a fresh bus: __len__ makes it falsy.
        self.bus = bus if bus is not None else InMemoryEventBus()
        self.grabber = FrameGrabber(
            source=camera.source,
            camera_id=camera.id,
            fps_limit=camera.fps_limit,
            loop_file=loop,
        )
        self.tracker = Tracker(
            model=self._model(),
            cfg=cfg.detection,
            enabled=track,
            footprint_ratio=cfg.occupancy.footprint_ratio,
        )
        self.engine = OccupancyEngine(
            camera.id,
            slots,
            cfg.occupancy,
            self.bus,
            identity_stable=track,
            # Without real track ids there is no motion history to gate on, so
            # Phase 3 behaviour (plain per-frame detections) is preserved.
            gate=VelocityGate(cfg.velocity_gating) if track else None,
        )
        self.frames_processed = 0
        self.last_summary = {"total": len(slots), "occupied": 0, "available": len(slots), "unknown": 0}
        # A served pipeline is long-lived, so the API needs a way to end a run
        # between frames: see `request_stop`.
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Ask the current ``run()`` to finish at the next frame boundary.

        The API's camera restart uses this instead of killing the thread: a run
        owns a decoder, a writer and OpenCV windows, and all of them are released
        in its ``finally`` block. Stopping mid-frame would leak them. A pipeline
        that has been stopped stays stopped - the server builds a new one.
        """
        self._stop_event.set()

    def _model(self):
        from smart_parking.detection.vehicle_detector import load_model

        return load_model(self.cfg.detection_model_path())

    def run(
        self,
        max_frames: int | None = None,
        show: bool = False,
        save_video: str | Path | None = None,
        save_frames_dir: str | Path | None = None,
        frame_stride: int | None = None,
        replay: bool = True,
        on_frame: Callable[[FrameView], None] | None = None,
        pace: bool = False,
    ) -> dict:
        """Process a source and return throughput plus the final summary.

        ``replay=True`` on a file source reads frames inline so every frame is
        seen. The threaded grabber keeps only the newest frame - correct for a
        live camera, but it silently drops most of a clip when inference lags
        behind the reader, which would corrupt any accuracy measurement.

        ``pace=True`` waits out the rest of each frame's own period, so a served
        replay is the clip's frame rate rather than the decoder's fastest. A CLI
        leaves it off: the number it prints is throughput.

        ``on_frame`` receives a ``FrameView`` holding the **annotated** frame - the
        same picture ``--show`` puts in the operator window - plus this camera's
        occupancy summary. It is how the API's MJPEG and overlay endpoints get a
        frame without this loop learning anything about HTTP: one drawing path, so
        a browser cannot be shown a different picture from the one an operator
        would have seen. Called on the pipeline thread, so a sink must be quick and
        must not raise.
        """
        stride = frame_stride or self.cfg.detection.frame_stride
        live_mode = not (replay and is_file_source(self.camera.source))
        looping = self.grabber.loop_file

        grabber = None
        cap = None
        seconds_per_frame = 0.0
        t_start = time.time()
        if live_mode:
            grabber = self.grabber.start()
        else:
            cap = open_capture(self.camera.source)
            # The FSM timers measure how long a car sits in a bay, so replay
            # must hand them video time. Advancing them with wall-clock time
            # would make a 3x-speed replay look like cars leaving 3x faster.
            speed = cap.get(cv2.CAP_PROP_FPS) or 0.0
            if speed > 0:
                seconds_per_frame = 1.0 / speed
            else:
                log.warning("%s: no fps on %s - timing replay in wall-clock", self.camera.id, self.camera.source)
                seconds_per_frame = 0.0

        def clock() -> float:
            if live_mode or seconds_per_frame == 0.0:
                return time.time()
            return t_start + (seq + 1) * seconds_per_frame

        # Counted off a running deadline, not off `seq`: `--loop` rewinds the clip
        # while the frame index keeps climbing, and the two would then disagree
        # about when the next frame is due.
        next_due = t_start

        def pace_frame() -> None:
            nonlocal next_due
            if not pace or live_mode or seconds_per_frame == 0.0:
                return
            next_due += seconds_per_frame
            delay = next_due - time.time()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                # Recover by moving the deadline, never by dropping a frame: a
                # frame skipped here is a bay nobody looked at.
                next_due = time.time()

        writer = None
        seq = -1
        t_warm: float | None = None
        t_fps_mark = time.time()
        frames_at_mark = 0
        fps = 0.0

        try:
            while True:
                if self._stop_event.is_set():
                    log.info("%s: run stopped by request", self.camera.id)
                    break
                if max_frames and self.frames_processed >= max_frames:
                    break

                if live_mode:
                    frame, seq_now = grabber.latest(seq)
                    if frame is None:
                        if grabber.status is GrabberStatus.STOPPED:
                            break
                        if grabber.is_stale() and time.time() - t_start > OFFLINE_AFTER_S:
                            # Never guess FREE for a dead camera.
                            self.engine.mark_all(SlotStatus.UNKNOWN, time.time())
                            log.error(
                                "%s: no frames for %.1fs - slots marked UNKNOWN",
                                self.camera.id,
                                OFFLINE_AFTER_S,
                            )
                            break
                        time.sleep(0.005)
                        continue
                    seq = seq_now
                else:
                    ok, frame = cap.read()
                    if not ok:
                        if looping:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            continue
                        log.info("%s: end of clip after %d frames", self.camera.id, seq)
                        break
                    seq += 1

                if stride > 1 and self.frames_processed % stride:
                    # Still run the FSMs on skipped frames so dwell timers advance
                    # with the clock, just without new evidence.
                    self._tick_only(clock())
                    pace_frame()
                    continue

                tracks = self.tracker.update(frame)
                states = self.engine.update(tracks, clock(), (frame.shape[1], frame.shape[0]))
                self.frames_processed += 1
                if t_warm is None:
                    t_warm = time.time()
                self.last_summary = self.engine.summary()

                if show or save_video or save_frames_dir or on_frame is not None:
                    annotated = frame.copy()
                    draw_slots(annotated, self.slots, {s.slot_id: s for s in states})
                    occupied_tracks = {
                        s.track_id for s in states if s.status is SlotStatus.OCCUPIED and s.track_id
                    }
                    draw_tracks(annotated, tracks, occupied_tracks)
                    draw_hud(annotated, self.last_summary, fps, f"{self.camera.name or self.camera.id}  frame {seq}")

                    if save_video:
                        writer = writer or _make_writer(save_video, annotated.shape)
                        writer.write(annotated)
                    if save_frames_dir:
                        _save_frame(annotated, save_frames_dir, self.frames_processed)
                    if on_frame is not None:
                        try:
                            on_frame(
                                FrameView(
                                    camera_id=self.camera.id,
                                    seq=seq,
                                    timestamp=clock(),
                                    image=annotated,
                                    tracks=tracks,
                                    # draw_slots colours the bays instead of writing
                                    # text on them, so the status map is this
                                    # camera's overlay payload.
                                    summary={
                                        **self.last_summary,
                                        "slots": {s.slot_id: s.status.value for s in states},
                                    },
                                )
                            )
                        except Exception:  # a browser that went away cannot stop inference
                            log.exception("%s: on_frame sink failed; frame dropped", self.camera.id)
                    if show:
                        cv2.imshow("Smart Parking - LOT", annotated)
                        if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                            break

                elapsed = time.time() - t_fps_mark
                if elapsed >= 1.0:
                    fps = (self.frames_processed - frames_at_mark) / elapsed
                    t_fps_mark, frames_at_mark = time.time(), self.frames_processed
                pace_frame()

        finally:
            if grabber is not None:
                grabber.stop()
            if cap is not None:
                cap.release()
            if writer is not None:
                writer.release()
                log.info("wrote %s", save_video)
            if show:
                cv2.destroyAllWindows()

        duration = max(time.time() - t_start, 1e-6)
        steady = max(time.time() - (t_warm or t_start), 1e-6)
        warm = max((t_warm or t_start) - t_start, 0.0)
        return {
            "frames": self.frames_processed,
            "seconds": duration,
            "fps": self.frames_processed / duration,
            # Ultralytics builds its predictor on the first call, which costs
            # seconds and makes the overall mean look far worse than reality.
            "warmup_seconds": warm,
            "steady_fps": (self.frames_processed - 1) / steady if self.frames_processed > 1 else 0.0,
            "summary": self.last_summary,
            "events": len(self.bus),
        }

    def _tick_only(self, now: float) -> None:
        """Advance dwell timers on a frame we skipped inference for."""
        self.engine.tick(now)
        self.frames_processed += 1
        self.last_summary = self.engine.summary()


def _make_writer(path: str | Path, shape) -> cv2.VideoWriter:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    h, w = shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(p), fourcc, 12.0, (w, h))
    if not writer.isOpened():
        log.warning("mp4v writer refused %s - falling back to .avi", p)
        writer = cv2.VideoWriter(str(p.with_suffix(".avi")), cv2.VideoWriter_fourcc(*"MJPG"), 12.0, (w, h))
    return writer


def _save_frame(frame, directory: str | Path, index: int) -> None:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    if index % 30 == 0:  # one sample per second at 30 fps source
        cv2.imwrite(str(d / f"frame_{index:06d}.jpg"), frame)
