"""The live layer: a pipeline thread's frames and events, handed to browsers.

Three objects, all of which exist because of one constraint: inference runs on
its own thread per camera and a WebSocket belongs to the event loop. Nothing here
may slow the inference thread down, and nothing here may raise into it - the
event bus swallows subscriber exceptions (``core/events.py``), so a bug in this
module would otherwise look like "the dashboard silently stopped updating".

* ``FrameHub`` keeps the newest ``FrameView`` per camera. Encoding happens when a
  viewer asks, so a camera nobody is watching costs nothing.
* ``WebSocketHub`` subscribes to the bus and pushes per-connection bounded
  queues onto the loop with ``call_soon_threadsafe``. A slow browser is dropped,
  not waited for.
* ``CameraRunner`` / ``CameraManager`` own the threads that run the pipelines. A
  served replay is paced at its clip's own frame rate, and only its first pass
  publishes events (``QuietBus``): a loop is a picture to watch, not new visits.

``mask_plate`` is applied per connection, from the role the token carried: one
event, several viewers, different pictures of the same plate - which is why the
payload is built per subscriber instead of once in ``_on_event``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import cv2
import numpy as np

from smart_parking.api.security import PLATE_REVEAL_ROLES, mask_plate
from smart_parking.core.config import (
    AppConfig,
    CameraConfig,
    CameraRole,
    MjpegConfig,
    ServeCameraConfig,
    gate_config_for,
    load_slots,
)
from smart_parking.core.events import EventBus
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import (
    CAMERA_STATUS,
    PLATE_READ,
    SLOT_FREED,
    SLOT_OCCUPIED,
    SLOT_SNAPSHOT,
    SUMMARY,
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    VIOLATION,
    Event,
    FrameView,
    Role,
)
from smart_parking.parking.geometry import slots_from_config
from smart_parking.pipeline.gate_pipeline import GatePipeline
from smart_parking.pipeline.lot_pipeline import LotPipeline
from smart_parking.tracking.replay import load_track_file

log = get_logger("api.live")

# A join() that outlives this is reported, not waited on: the pipeline checks the
# stop flag once per frame, so a stuck decoder is a bug to surface, not a reason
# to hang the shutdown.
STOP_TIMEOUT_S = 10.0


def _encode(image: np.ndarray, mjpeg: MjpegConfig) -> bytes:
    """Scale to the stream width and JPEG-encode.

    The plate crop is taken inside the pipeline, never from this picture, so
    downscaling here costs a viewer's detail and no evidence.
    """
    h, w = image.shape[:2]
    if mjpeg.max_width and w > mjpeg.max_width:
        scale = mjpeg.max_width / float(w)
        image = cv2.resize(image, (mjpeg.max_width, max(1, int(h * scale))))
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), mjpeg.quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


class FrameHub:
    """Newest annotated frame per camera, encoded on demand.

    Encoding inside ``put`` would mean every processed frame costs a JPEG for
    every camera whether or not anyone is watching; the stream generators call
    ``jpeg_since`` at their own pace instead, so the bill is per viewer per
    frame actually delivered.
    """

    def __init__(self, mjpeg: MjpegConfig) -> None:
        self.mjpeg = mjpeg
        self._lock = threading.Lock()
        self._views: dict[str, FrameView] = {}
        self.frames_in = 0
        self.encodes = 0

    def put(self, view: FrameView) -> None:
        # Replace, never merge: the pipeline thread allocates a new annotated
        # array each frame, so the stored array is never mutated under us.
        with self._lock:
            self._views[view.camera_id] = view
            self.frames_in += 1

    def view(self, camera_id: str) -> FrameView | None:
        with self._lock:
            return self._views.get(camera_id)

    def cameras(self) -> list[str]:
        with self._lock:
            return sorted(self._views)

    def jpeg_since(self, camera_id: str, after_seq: int | None) -> tuple[int, bytes] | None:
        """`(seq, jpeg)` for the newest frame after `after_seq`, or None.

        None is the answer a caller waits on: "no new frame", not an error.
        """
        with self._lock:
            view = self._views.get(camera_id)
        if view is None or (after_seq is not None and view.seq <= after_seq):
            return None
        data = _encode(view.image, self.mjpeg)
        with self._lock:
            self.encodes += 1
        return view.seq, data

    def snapshot(self, camera_id: str) -> bytes | None:
        """One JPEG at the source's own width, for the endpoint that saves a picture."""
        with self._lock:
            view = self._views.get(camera_id)
            if view is None:
                return None
            self.encodes += 1
            ok, buf = cv2.imencode(
                ".jpg", view.image, [int(cv2.IMWRITE_JPEG_QUALITY), self.mjpeg.quality]
            )
        return buf.tobytes() if ok else None


class Subscription:
    """One WebSocket viewer's queue, plus what that viewer is allowed to see."""

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int, reveal_plates: bool) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.reveal_plates = reveal_plates
        self.delivered = 0
        self.dropped = 0

    def offer(self, payload: dict[str, Any]) -> None:
        """Run on the event loop. Full means the viewer is too slow, so drop."""
        if self.queue.full():
            self.dropped += 1
            return
        self.queue.put_nowait(payload)
        self.delivered += 1


_CHANNELS = {
    SLOT_OCCUPIED: "slot",
    SLOT_FREED: "slot",
    SLOT_SNAPSHOT: "slot",
    SUMMARY: "summary",
    VEHICLE_ENTERED: "session",
    VEHICLE_EXITED: "session",
    VIOLATION: "violation",
    PLATE_READ: "plate",
    CAMERA_STATUS: "camera",
}


def _payload_for(event: Event, reveal_plates: bool) -> dict[str, Any]:
    payload = dict(event.payload)
    if not reveal_plates:
        for key in ("plate", "plate_before", "plate_after"):
            if payload.get(key):
                payload[key] = mask_plate(payload[key])
    return {
        "type": event.type,
        # The doc's `/ws/live` is "one multiplexed channel for the dashboard", so
        # a client that only cares about bays can filter on `channel` instead of
        # listing every event type it knows about. `type` stays the bus's own
        # vocabulary (`slot_occupied`, not the doc's illustrative `slot_update`):
        # renaming a contract at the last hop is how a dashboard ends up parsing
        # names that appear nowhere else in the system.
        "channel": _CHANNELS.get(event.type, "other"),
        "camera_id": event.camera_id,
        "ts": event.timestamp,
        "payload": payload,
    }


class WebSocketHub:
    """Bus -> per-connection queues, bridging threads on the way.

    ``_on_event`` is the bus's subscriber, so it runs on the inference thread and
    must never raise: the bus swallows exceptions from subscribers, which would
    turn a bug here into an event that simply never reached a dashboard.
    """

    def __init__(self, bus: EventBus, api, *, include_summary: bool = True) -> None:
        self.api = api
        self.include_summary = include_summary
        self._subs: set[Subscription] = set()
        self._lock = threading.Lock()
        self.events_in = 0
        self.dropped = 0
        bus.subscribe(self._on_event)

    def connect(self, role: Role | str, *, loop: asyncio.AbstractEventLoop) -> Subscription:
        reveal = Role(role).value in PLATE_REVEAL_ROLES
        sub = Subscription(loop, self.api.ws_queue_size, reveal)
        with self._lock:
            self._subs.add(sub)
        return sub

    def disconnect(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)
        self.dropped += sub.dropped

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._subs)

    def _on_event(self, event: Event) -> None:
        if event.type == SUMMARY and not self.include_summary:
            return
        with self._lock:
            subs = list(self._subs)
        if not subs:
            return
        self.events_in += 1
        for sub in subs:
            try:
                payload = _payload_for(event, sub.reveal_plates)
            except Exception:  # a bad payload is one viewer's problem, not the gate's
                log.exception("ws: could not serialise %s", event.type)
                continue
            try:
                # call_soon_threadsafe is what makes this legal from another thread.
                sub.loop.call_soon_threadsafe(sub.offer, payload)
            except RuntimeError:
                # The loop closed while this event was in flight: the viewer left,
                # so its undelivered events are counted as drops, not lost silently.
                # The one lost here is added to the *subscriber's* count, because
                # `disconnect` rolls that count into the hub's - adding to both
                # would report one dead browser as two dropped events.
                sub.dropped += 1
                self.disconnect(sub)


class QuietBus:
    """A bus that publishes nothing, for the passes of a loop that are only a picture.

    The first pass of a served replay is a run worth recording. The second pass of
    the same clip is the same four vehicles crossing the same line again, under the
    same `source_session_id` - which the database refuses, correctly, because an
    idempotency key that accepted it would be a duplicate visit. Fed to the real
    bus that is an endless page of errors; on a looping camera it never stops. So
    the runner hands the pipeline this instead, and the frames keep flowing through
    `on_frame` while the events do not.

    It is a subscriber's shape, not a subscriber: nothing can listen to a run whose
    events are being dropped, which is the point.
    """

    def publish(self, event: Event) -> None:
        return None

    def subscribe(self, handler: Any) -> Any:  # pragma: no cover - pipelines never call this
        return handler

    def drain(self, timeout: float = 0.0) -> list[Event]:
        return []

    def __len__(self) -> int:
        return 0


class CameraRunner:
    """One camera's pipeline on one thread, for as long as the server lives."""

    def __init__(
        self,
        cfg: AppConfig,
        camera: CameraConfig,
        entry: ServeCameraConfig,
        bus: EventBus,
        frames: FrameHub,
        ocr: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.camera = camera
        self.entry = entry
        self.bus = bus
        self.frames = frames
        # Built by the caller because an OCR engine is the expensive object in the
        # process; None means this camera does not read plates at all.
        self.ocr = ocr
        self._replay = None
        if entry.tracks_file:
            by_frame, _expected = load_track_file(entry.tracks_file, cfg)
            self._replay = lambda frame: by_frame.get(frame, [])
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._pipeline = None
        self.passes = 0
        self.error: str | None = None
        self.started_at: float | None = None

    # -- pipeline construction ----------------------------------------------
    def _build(self, bus: Any | None = None):
        """The pipeline for one pass. `bus` defaults to the app's; a later pass of
        a looping replay gets a :class:`QuietBus` (see ``_run``)."""
        bus = self.bus if bus is None else bus
        if self.camera.role is CameraRole.LOT:
            if not self.camera.slots_file:
                raise RuntimeError(f"camera {self.camera.id} is a LOT camera with no slots_file")
            slots = slots_from_config(
                load_slots(self.cfg.resolve(self.camera.slots_file), self.cfg.project_root),
                self.camera.id,
            )
            return LotPipeline(
                self.cfg,
                self.camera,
                slots,
                bus=bus,
                # A labelled replay has no detections to pair with, so ByteTrack
                # would only invent ids over the top of the ones already given.
                track=self._replay is None,
                loop=self.entry.loop,
            )
        gate = gate_config_for(self.cfg, self.camera)
        return GatePipeline(
            self.cfg,
            self.camera,
            gate=gate,
            bus=bus,
            anpr=self.entry.read_plates,
            ocr=self.ocr if self.entry.read_plates else None,
            replay_tracks=self._replay,
        )

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._closed.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.camera.id}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        pace = self.cfg.api.pipeline.pace_replay
        quiet = self.cfg.api.pipeline.first_pass_events_only and self.entry.loop
        while not self._closed.is_set():
            # Only the first pass of a looping replay reaches the bus. See QuietBus.
            bus = self.bus if not quiet or self.passes == 0 else QuietBus()
            try:
                pipeline = self._build(bus)
            except Exception as exc:  # a camera without its files is not a dead server
                self.error = f"build failed: {exc}"
                log.exception("%s: could not build pipeline", self.camera.id)
                return
            self._pipeline = pipeline
            try:
                pipeline.run(
                    replay=self.cfg.api.pipeline.replay,
                    frame_stride=self.cfg.api.pipeline.frame_stride,
                    on_frame=self.frames.put,
                    pace=pace,
                )
            except Exception as exc:
                self.error = f"run failed: {exc}"
                log.exception("%s: pipeline raised", self.camera.id)
                return
            finally:
                self._pipeline = None
            self.passes += 1
            if not self.entry.loop or self._closed.is_set():
                break
        log.info("%s: stream thread finished after %d pass(es)", self.camera.id, self.passes)

    def stop(self, *, timeout: float = STOP_TIMEOUT_S) -> bool:
        """End the run and join. True means the thread is really gone."""
        self._closed.set()
        pipeline = self._pipeline
        if pipeline is not None:
            pipeline.request_stop()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("%s: stream thread still running after %.1fs", self.camera.id, timeout)
            return False
        self._thread = None
        return True

    def restart(self, *, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Stop cleanly, then start a fresh pipeline - the doc's camera restart."""
        ok = self.stop(timeout=timeout)
        if ok:
            self.start()
        return ok

    def status(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera.id,
            "role": self.camera.role.value,
            "running": self._thread is not None and self._thread.is_alive(),
            "synthetic_replay": self._replay is not None,
            "read_plates": self.entry.read_plates,
            "loop": self.entry.loop,
            "passes": self.passes,
            "frames_processed": getattr(self._pipeline, "frames_processed", None),
            "error": self.error,
        }


class CameraManager:
    """The set of runners the lifespan owns."""

    def __init__(self, cfg: AppConfig, bus: EventBus, frames: FrameHub) -> None:
        self.cfg = cfg
        self.bus = bus
        self.frames = frames
        self._runners: dict[str, CameraRunner] = {}
        self._lock = threading.Lock()
        self._ocr: Any | None = None
        self._ocr_tried = False

    def _ocr_engine(self) -> Any | None:
        if self._ocr_tried:
            return self._ocr
        self._ocr_tried = True
        if not self.cfg.anpr.enabled:
            return None
        from smart_parking.anpr.engine import build_ocr

        self._ocr = build_ocr(self.cfg.anpr)
        return self._ocr

    def start(self) -> list[str]:
        """Start every configured camera; returns the ids that came up."""
        started: list[str] = []
        for entry in self.cfg.api.pipeline.cameras:
            camera = next((c for c in self.cfg.cameras if c.id == entry.camera), None)
            if camera is None:
                log.error("api.pipeline.cameras lists %s, which is not in cameras.yaml", entry.camera)
                continue
            if not camera.source or not self.cfg.resolve(camera.source).exists():
                log.error("camera %s: source %s is not readable - not started", camera.id, camera.source)
                continue
            ocr = self._ocr_engine() if entry.read_plates else None
            runner = CameraRunner(self.cfg, camera, entry, self.bus, self.frames, ocr=ocr)
            with self._lock:
                self._runners[camera.id] = runner
            runner.start()
            started.append(camera.id)
        return started

    def runners(self) -> list[CameraRunner]:
        with self._lock:
            return list(self._runners.values())

    def get(self, camera_id: str) -> CameraRunner | None:
        with self._lock:
            return self._runners.get(camera_id)

    def statuses(self) -> list[dict[str, Any]]:
        return [r.status() for r in self.runners()]

    def stop_all(self, *, timeout: float = STOP_TIMEOUT_S) -> None:
        for runner in self.runners():
            runner.stop(timeout=timeout)
