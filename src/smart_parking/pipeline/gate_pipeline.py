"""GATE camera loop: frames -> vehicles -> plates -> crossings -> sessions (Phase 6).

    frame -> tracks (YOLO + ByteTrack, or a labelled replay)
          -> plates (AnprPipeline, Phase 5)
          -> gate line crossings (GateLogic)
          -> parking sessions with durations (SessionService)

The three stages keep their own boundaries: this module owns neither the crossing
rules nor the session lifecycle, it only feeds each of them the camera's clock and
passes the events along. That is what lets the same code run against a real
barrier feed and against a rendered fixture whose boxes are handed in.

No slots, no occupancy, no database: the lot camera owns those (Phases 1-4) and a
gate camera has no view of the bays. ``bind_slot`` stays uncalled until something
can honestly say which bay a gate track parked in.

Timing is always video time, never wall-clock time, for the same reason the lot
pipeline does it: a session's duration is the visit, not how fast this machine
managed to decode the clip. See ``run``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from smart_parking.anpr.pipeline import AnprPipeline
from smart_parking.core.config import AppConfig, CameraConfig, GateConfig, gate_config_for
from smart_parking.core.events import EventBus, InMemoryEventBus
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import Event, FrameView, Track
from smart_parking.parking.gate_logic import GateLogic
from smart_parking.parking.render import draw_gate
from smart_parking.services.session_service import SessionService
from smart_parking.tracking.tracker import Tracker
from smart_parking.video.grabber import FrameGrabber, GrabberStatus
from smart_parking.video.sources import is_file_source, open_capture

log = get_logger("pipeline.gate")

# A camera that has not delivered a frame for this long is considered down.
OFFLINE_AFTER_S = 5.0

# frame index -> the tracks that were seen on it. Supplied by a replay driver
# (a labelled box file) so a fixture without real cars still exercises the gate.
ReplayTracks = Callable[[int], list[Track]]
# Session events as they happen, for a CLI that prints ENTER/EXIT live rather
# than reconstructing them from the bus afterwards.
EventSink = Callable[[Event], None]


class GatePipeline:
    def __init__(
        self,
        cfg: AppConfig,
        camera: CameraConfig,
        gate: GateConfig | None = None,
        bus: EventBus | None = None,
        track: bool = True,
        anpr: bool = True,
        ocr=None,
        replay_tracks: ReplayTracks | None = None,
        on_event: EventSink | None = None,
    ) -> None:
        self.cfg = cfg
        self.camera = camera
        # The gate config carries both the geometry (from the camera's gate file)
        # and the timers; a caller that wants different timers passes a modified
        # copy rather than a second knob here.
        self.gate = gate if gate is not None else gate_config_for(cfg, camera)
        # `bus or ...` would discard a fresh bus: InMemoryEventBus defines __len__.
        self.bus = bus if bus is not None else InMemoryEventBus()
        self.replay_tracks = replay_tracks
        self.on_event = on_event
        self.tracker = (
            None
            if replay_tracks is not None or not track
            else Tracker(model=self._model(), cfg=cfg.detection, enabled=True)
        )
        self.anpr = (
            None
            if not anpr or not cfg.anpr.enabled
            else AnprPipeline(cfg, camera.id, bus=self.bus, ocr=ocr)
        )
        self.logic = GateLogic(self.gate, camera.id)
        self.sessions = SessionService(self.gate, self.bus)
        self.grabber = FrameGrabber(
            source=camera.source,
            camera_id=camera.id,
            fps_limit=camera.fps_limit,
            loop_file=False,
        )
        self.frames_processed = 0
        self.crossings_seen = 0
        self.last_events: list[Event] = []
        # See LotPipeline.request_stop: the API restarts a gate camera by ending its
        # run cleanly, not by abandoning a thread that holds a decoder open.
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Ask the current ``run()`` to finish at the next frame boundary."""
        self._stop_event.set()

    def _model(self):
        from smart_parking.detection.vehicle_detector import load_model

        return load_model(self.cfg.detection_model_path())

    # -- one frame ----------------------------------------------------------
    def process(
        self, frame: np.ndarray, tracks: list[Track], now: float, frame_size: tuple[int, int]
    ) -> list[Event]:
        """Plates first, then crossings: a track that reads its plate on the very
        frame it reaches the line should enter with the plate already bound."""
        if self.anpr is not None:
            for result in self.anpr.process(frame, tracks, now):
                self.sessions.plate(result, self.camera.id)
        crossings = self.logic.update(tracks, now, frame_size)
        crossings.extend(self.logic.prune(now))
        events: list[Event] = []
        for cross in crossings:
            self.crossings_seen += 1
            events.extend(self.sessions.crossing(cross))
        self.sessions.prune(now)
        self.frames_processed += 1
        self.last_events = events
        self._emit(events)
        return events

    def tick(self, now: float) -> list[Event]:
        """Advance the gate clock on a frame inference was skipped for.

        The dwell timer measures how long a vehicle has been *gone*, which keeps
        running whether or not this machine processed the frame. Feeding it no
        tracks is exactly right: absence of evidence, not evidence of absence.
        """
        events: list[Event] = []
        for cross in self.logic.prune(now):
            self.crossings_seen += 1
            events.extend(self.sessions.crossing(cross))
        self.sessions.prune(now)
        self.frames_processed += 1
        self.last_events = events
        self._emit(events)
        return events

    # -- a whole source -----------------------------------------------------
    def run(
        self,
        max_frames: int | None = None,
        show: bool = False,
        save_video: str | Path | None = None,
        frame_stride: int | None = None,
        replay: bool = True,
        on_frame: Callable[[FrameView], None] | None = None,
        pace: bool = False,
    ) -> dict:
        """Process the camera's source and return throughput plus the sessions.

        ``replay=True`` on a file source reads every frame inline. The threaded
        grabber keeps only the newest frame, which is correct for a live camera
        and silently drops most of a clip during a slow run - a dropped frame
        here is a missing crossing, not just a coarser picture.

        ``pace=True`` waits out the remainder of each frame's own period instead of
        reading as fast as the decoder allows. A CLI wants the fastest run it can
        get, because the number it prints is throughput; a served stream wants the
        clip's speed, because the picture it prints is a 15 fps video and an
        unpaced loop is a flicker. The ``fps`` this returns is then the clip's, not
        the machine's - which is why nothing calls it a frame rate.

        ``on_frame`` receives a ``FrameView`` holding the **annotated** frame - the
        same picture ``--show`` puts in the operator window - plus the boxes and the
        per-track text drawn on them, so the API's stream and its overlay JSON
        cannot diverge into two drawing paths. Called on the pipeline thread: a
        sink has to be quick and must not raise.
        """
        stride = max(1, frame_stride or self.cfg.detection.frame_stride)
        live_mode = not (replay and is_file_source(self.camera.source))
        source = self.camera.source

        grabber = None
        cap = None
        seconds_per_frame = 0.0
        t_start = time.time()
        if live_mode:
            grabber = self.grabber.start()
        else:
            cap = open_capture(source)
            speed = cap.get(cv2.CAP_PROP_FPS) or 0.0
            if speed > 0:
                seconds_per_frame = 1.0 / speed
            else:
                log.warning(
                    "%s: no fps on %s - timing the gate in wall-clock", self.camera.id, source
                )

        def clock() -> float:
            if live_mode or seconds_per_frame == 0.0:
                return time.time()
            return t_start + (seq + 1) * seconds_per_frame

        # Counted off a running deadline rather than off `seq`, because a looping
        # source keeps its frame index climbing while the clip restarts - the two
        # would disagree about when the next frame is due.
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
                # Catch up by moving the deadline, never by skipping a frame: on a
                # gate a skipped frame is a crossing that did not happen.
                next_due = time.time()

        writer = None
        seq = -1
        t_warm: float | None = None
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
                            # Stop, and stop the clock too: a dead camera has not
                            # lost its vehicles, so the dwell timer must not be
                            # allowed to turn an outage into a fleet of exits.
                            log.error(
                                "%s: no frames for %.1fs - stopping without closing sessions",
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
                        log.info("%s: end of clip after %d frames", self.camera.id, seq)
                        break
                    seq += 1

                now = clock()
                frame_size = (frame.shape[1], frame.shape[0])
                if stride > 1 and seq % stride:
                    self.tick(now)
                    pace_frame()
                    continue

                if self.tracker is not None:
                    tracks = self.tracker.update(frame)
                elif self.replay_tracks is not None:
                    tracks = self.replay_tracks(seq)
                else:
                    tracks = []
                self.process(frame, tracks, now, frame_size)
                if t_warm is None:
                    t_warm = time.time()

                if show or save_video or on_frame is not None:
                    annotated = frame.copy()
                    labels = self.labels(tracks)
                    # Short enough to fit a 480px gate picture: the camera name and
                    # the geometry are in the run header, this is the live counter.
                    hud = (
                        f"frame {seq}  crossings {self.crossings_seen}  "
                        f"sessions {len(self.sessions.sessions)}"
                        + ("  SYNTHETIC REPLAY" if self.replay_tracks is not None else "")
                    )
                    draw_gate(annotated, self.gate, tracks, labels, hud)
                    if save_video:
                        writer = writer or _make_writer(save_video, annotated.shape)
                        writer.write(annotated)
                    if on_frame is not None:
                        try:
                            on_frame(
                                FrameView(
                                    camera_id=self.camera.id,
                                    seq=seq,
                                    timestamp=now,
                                    image=annotated,
                                    tracks=tracks,
                                    labels=labels,
                                    summary={
                                        "crossings": self.crossings_seen,
                                        "open_sessions": len(self.sessions.open_sessions()),
                                        "sessions": len(self.sessions.sessions),
                                        "hud": hud,
                                    },
                                )
                            )
                        except Exception:  # a viewer that went away cannot stop a gate
                            log.exception("%s: on_frame sink failed; frame dropped", self.camera.id)
                    if show:
                        cv2.imshow("Smart Parking - GATE", annotated)
                        if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                            break
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
        return {
            "frames": self.frames_processed,
            "seconds": duration,
            "fps": self.frames_processed / duration,
            # Ultralytics builds its predictor on the first call, which costs
            # seconds and makes the overall mean look far worse than reality.
            "warmup_seconds": max((t_warm or t_start) - t_start, 0.0),
            "steady_fps": (self.frames_processed - 1) / steady if self.frames_processed > 1 else 0.0,
            "crossings": self.crossings_seen,
            "duplicate_crossings": self.sessions.duplicate_crossings,
            "sessions": len(self.sessions.sessions),
            "open_sessions": len(self.sessions.open_sessions()),
            "events": len(self.bus.recorded) if hasattr(self.bus, "recorded") else len(self.bus),
        }

    # -- operator view ------------------------------------------------------
    def _emit(self, events: list[Event]) -> None:
        if self.on_event is None:
            return
        for event in events:
            self.on_event(event)

    def labels(self, tracks: list[Track]) -> dict[int, str]:
        """What the gate currently believes about each vehicle in view."""
        open_by_track = {s.track_id: s for s in self.sessions.open_sessions()}
        out: dict[int, str] = {}
        for track in tracks:
            session = open_by_track.get(track.track_id)
            plate = None
            if self.anpr is not None:
                result = self.anpr.plate_for(track.track_id)
                plate = result.plate_text if result is not None else None
            if plate is None and session is not None:
                plate = session.plate
            bits = [plate or "no plate"]
            if session is not None:
                bits.append(session.session_id.split("-")[-1])
                if session.review_reason:
                    bits.append("review")
                if session.anomaly_reason:
                    bits.append("anomaly")
            out[track.track_id] = " ".join(bits)
        return out


def _make_writer(path: str | Path, shape) -> cv2.VideoWriter:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    h, w = shape[:2]
    writer = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (w, h))
    if not writer.isOpened():
        log.warning("mp4v writer refused %s - falling back to .avi", p)
        writer = cv2.VideoWriter(str(p.with_suffix(".avi")), cv2.VideoWriter_fourcc(*"MJPG"), 12.0, (w, h))
    return writer
