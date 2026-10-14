"""Persistence (Phase 7): the events the pipeline emits become rows.

The chain ends here::

    YOLO -> tracking -> occupancy -> ANPR -> gate crossings -> sessions
        -> this module -> repositories -> SQLite

Nothing under ``detection/``, ``parking/``, ``tracking/``, ``anpr/`` or
``pipeline/`` changes for it: they already publish ``Event`` objects on the bus,
and this module is just another consumer. That is what keeps the AI layers free of
SQLAlchemy - and it is why every number in a session row can be traced back to a
line the Phase 6 CLI already printed.

**Why a thread.** ``InMemoryEventBus`` is a bounded queue, so the pipeline never
waits for a consumer: a worker drains it on its own thread and writes a batch per
transaction. Writing inline would have SQLite on the frame path, where a 6 GB GPU
inference loop is timed in tens of milliseconds. A queue of the same shape would
mean a second queue next to the one that already exists, so there isn't one.

**Why failures are loud.** The bus deliberately swallows subscriber exceptions to
protect inference, so a consumer that also swallowed them would let the pipeline
report sessions it never wrote. Instead: a batch that fails is retried one event at
a time (one bad event cannot roll back the good ones), the surviving error is
logged with its traceback, recorded in :attr:`PersistenceWorker.failures`, and
printed by the CLI at the end of the run. That list is a bounded sample, because a
server runs for a shift where a CLI runs for one clip; :attr:`failure_total` beside
it is the real count and is what `stats` reports, so a cap on memory is not a cap on
what the operator is told.

**Timestamps.** The domain keeps time as epoch floats (video time on a replay);
every datetime in the database is naive UTC. The conversion happens here, once, at
the seam - see ``db/base.py``.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

from smart_parking.anpr.validation import PlateValidator
from smart_parking.core.config import AppConfig, load_slots
from smart_parking.core.events import EventBus
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import (
    NO_ENTRY,
    PLATE_READ,
    SLOT_FREED,
    SLOT_OCCUPIED,
    SLOT_SNAPSHOT,
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    VIOLATION,
    Event,
    SessionStatus,
    SlotStatus,
)
from smart_parking.db.base import to_utc
from smart_parking.db.models import ParkingSession as SessionRow
from smart_parking.db.repositories import Repositories
from smart_parking.db.session import Database

log = get_logger("services.persistence")

Normaliser = Callable[[str], str]


def plate_normaliser_for(cfg: AppConfig) -> Normaliser:
    """The Phase 5 validator's own normalisation, as a callable.

    Deliberately not a second implementation: the voting stage decided what a
    plate *is*, and a database layer that folded `MH-20-AB-1234` a different way
    would put one car in two rows.
    """
    return PlateValidator(cfg.anpr.validation).normalise


# -- configuration rows -----------------------------------------------------
def sync_configuration(database: Database, cfg: AppConfig, *, normalise: Normaliser) -> int:
    """Write the cameras and calibrated bays the config files describe.

    Idempotent, and run before a pipeline starts: sessions and occupancy rows
    point at camera and slot rows by foreign key, so those have to exist before
    an observation does. Bay geometry is refreshed from the calibration file, which
    is the same file the engine reads, so the drawing stays the single source of
    truth. Returns how many bays were written.
    """
    bays = 0
    with database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        repos.cameras.sync_from_config(cfg.cameras)
        for camera in cfg.cameras:
            if not camera.slots_file:
                continue
            try:
                slots = load_slots(camera.slots_file, cfg.project_root)
            except FileNotFoundError:
                log.warning("%s: slots file %s missing, bays not synced", camera.id, camera.slots_file)
                continue
            bays += len(repos.slots.sync(camera.id, slots))
    return bays


# -- event -> row -----------------------------------------------------------
def apply_events(database: Database, events: Iterable[Event], *, normalise: Normaliser) -> int:
    """Write a batch of events as one transaction. Raises on failure.

    One transaction per batch is what makes a visit atomic: an entry and its exit
    may land in different batches, but a plate, a vehicle and a session written in
    the same batch either all appear or none do.
    """
    events = list(events)
    if not events:
        return 0
    with database.scope() as session:
        repos = Repositories(session, normalise=normalise)
        for event in events:
            apply_event(repos, event)
        _mark_cameras_seen(repos, events)
    return len(events)


def apply_event(repos: Repositories, event: Event) -> SessionRow | None:
    """Write one event and return the session row it touched, if any.

    Every type also goes in ``detection_logs`` with its payload intact, so a row
    in that table can be compared with the console line the pipeline printed.
    """
    handler = {
        VEHICLE_ENTERED: _on_entered,
        VEHICLE_EXITED: _on_exited,
        VIOLATION: _on_violation,
        PLATE_READ: _on_plate,
        SLOT_OCCUPIED: _on_slot_occupied,
        SLOT_FREED: _on_slot_freed,
        SLOT_SNAPSHOT: _on_slot_snapshot,
    }.get(event.type)
    row = handler(repos, event) if handler is not None else None
    repos.events.append(event, session_row_id=None if row is None else row.id)
    return row


def _on_entered(repos: Repositories, event: Event) -> SessionRow:
    p = event.payload
    return repos.sessions.open(
        p["session_id"],
        p["camera_id"],
        p["track_id"],
        to_utc(p["entry_ts"]),
        plate=_plate(repos, event),
        ocr_confidence=p.get("ocr_confidence"),
        review_reason=p.get("review_reason"),
        anomaly_reason=p.get("anomaly_reason"),
        # The state machine's own verdict, passed through. A duplicate-plate entry
        # arrives as ANOMALY, which is also the only way two live sessions can
        # share a plate without tripping the unique index.
        status=SessionStatus(p["status"]),
    )


def _on_exited(repos: Repositories, event: Event) -> SessionRow:
    p = event.payload
    if repos.sessions.get(p["session_id"]) is None:
        # The entry was never persisted (a worker started mid-run, or the bus
        # overflowed). The exit is still a fact, so it is written as an
        # exit-without-entry - never with an entry time guessed backwards from it.
        return _orphan(repos, event)
    row = repos.sessions.close(
        p["session_id"],
        to_utc(p["exit_ts"]),
        p.get("duration"),
        p.get("exit_reason") or "line",
        status=SessionStatus(p["status"]),
        exit_track_id=p.get("track_id"),
        review_reason=p.get("review_reason"),
    )
    plate = _plate(repos, event)
    if plate is not None and row.plate_id != plate.id:
        # The plate the visit ended with travels inside this event, so a
        # PLATE_READ the bus never delivered costs the vehicle row - the class
        # the detector reported is not in a session event - but not the identity
        # itself. No vehicle row is invented to fill that gap.
        row = repos.sessions.bind_plate(
            row,
            plate,
            ocr_confidence=p.get("ocr_confidence"),
            plate_confidence=p.get("plate_confidence"),
        )
    return row


def _on_violation(repos: Repositories, event: Event) -> SessionRow | None:
    """The two session anomalies, and the lot camera's straddling reports.

    A violation carrying ``session_id`` is the gate's; without one it is a parking
    violation the occupancy engine raised, and there is no session row to attach -
    the event log holds it, which is all this phase can honestly store.
    """
    p = event.payload
    if "session_id" not in p:
        return None
    kind = p.get("kind")
    if kind == NO_ENTRY:
        return _orphan(repos, event)
    row = repos.sessions.get(p["session_id"])
    if row is None:
        # A duplicate plate announced at the entry: the session begins as an
        # anomaly instead of becoming a second live claim on the plate.
        return _on_entered(repos, event)
    return repos.sessions.set_status(row, SessionStatus(p["status"]), reason=kind)


def _on_plate(repos: Repositories, event: Event) -> SessionRow | None:
    p = event.payload
    if not p.get("plate"):
        return None
    plate = repos.plates.record(
        p["plate"], to_utc(p.get("ts") or event.timestamp), p.get("ocr_confidence")
    )
    vehicle = repos.vehicles.record(plate, p.get("class"))
    row = repos.sessions.active_for(event.camera_id, p["track_id"])
    if row is None:
        # A plate for a vehicle that has not crossed a line yet. The plate is a
        # real observation, so it is kept; which visit it belongs to is the state
        # machine's job and it will bind it on the entry event.
        return None
    return repos.sessions.bind_plate(
        row,
        plate,
        ocr_confidence=p.get("ocr_confidence"),
        plate_confidence=p.get("plate_confidence"),
        vehicle=vehicle,
    )


def _on_slot_occupied(repos: Repositories, event: Event) -> None:
    slot = _slot(repos, event)
    if slot is None:
        return
    when = to_utc(event.timestamp)
    p = event.payload
    repos.slots.set_status(slot, SlotStatus(p["status"]), when, p.get("track_id"))
    # One open interval per bay: a repeated OCCUPIED report refreshes the state
    # but does not start a second stay in the same bay.
    if repos.occupancy.open_for(slot.id) is None:
        repos.occupancy.open_interval(slot, when, track_id=p.get("track_id"), score=p.get("score"))


def _on_slot_freed(repos: Repositories, event: Event) -> None:
    p = event.payload
    slot = _slot(repos, event)
    if slot is None:
        return
    when = to_utc(event.timestamp)
    repos.slots.set_status(slot, SlotStatus(p["status"]), when, None)
    # The interval closes with the FSM's own FREE event; a bay that never
    # committed an occupancy has no interval to close, and none is invented.
    repos.occupancy.close_interval(slot.id, when)


def _on_slot_snapshot(repos: Repositories, event: Event) -> None:
    """The engine's first reading of a bay, written exactly as a transition is.

    Routed through the two handlers above rather than given a third
    implementation: a bay an earlier run left occupied has to close that
    interval when this run sees it empty, and a bay already holding a car has to
    open one. Both are already true of a transition with the same payload.
    """
    if SlotStatus(event.payload["status"]) is SlotStatus.OCCUPIED:
        return _on_slot_occupied(repos, event)
    return _on_slot_freed(repos, event)


def _orphan(repos: Repositories, event: Event) -> SessionRow:
    p = event.payload
    return repos.sessions.record_orphan_exit(
        p["session_id"],
        p["camera_id"],
        p["track_id"],
        to_utc(p.get("exit_ts") or event.timestamp),
        p.get("exit_reason") or "line",
        p.get("anomaly_reason") or NO_ENTRY,
        plate=_plate(repos, event),
        ocr_confidence=p.get("ocr_confidence"),
    )


def _plate(repos: Repositories, event: Event):
    """The plate row for a session event, or None when nothing was read.

    A session with no plate is the normal case on footage whose plate was too
    small to read, and it is stored as a session with `plate_id` NULL - never
    with a number made up to fill the column.
    """
    payload = event.payload
    text = payload.get("plate")
    if not text:
        return None
    return repos.plates.record(
        text, to_utc(payload.get("entry_ts") or event.timestamp), payload.get("ocr_confidence")
    )


def _slot(repos: Repositories, event: Event):
    """The bay this event names, or None when the camera has no calibrated bays.

    A gate camera sees no bays, so a slot event from one is not a row to write;
    the event still reaches ``detection_logs`` with the rest.
    """
    return repos.slots.find(event.camera_id, event.payload.get("slot_id") or "")


def _mark_cameras_seen(repos: Repositories, events: list[Event]) -> None:
    """Last time this camera produced an observation, per batch.

    On a replay that is video time rather than the wall clock, which is the same
    honesty the sessions use: every timestamp in the database is the camera's
    clock, not this machine's.
    """
    latest: dict[str, float] = {}
    for event in events:
        latest[event.camera_id] = max(latest.get(event.camera_id, 0.0), event.timestamp)
    for camera_id, when in latest.items():
        repos.cameras.mark_seen(camera_id, to_utc(when))


# -- the worker -------------------------------------------------------------
class PersistenceWorker:
    """Drains the bus on its own thread and writes what it finds.

    :attr:`failures` is the part a caller must check: it is empty when every
    event was written, and each entry is the event type plus the database error
    that survived the retry. It is a sample of at most `max_failures_kept`
    entries; :attr:`failure_total` is the count behind it, and is what
    :meth:`stats` reports.
    """

    def __init__(
        self,
        database: Database,
        cfg: AppConfig,
        bus: EventBus,
        *,
        normalise: Normaliser | None = None,
        batch_size: int = 200,
        poll_interval: float = 0.05,
        linger: float = 0.1,
        max_failures_kept: int = 200,
    ) -> None:
        self.database = database
        self.cfg = cfg
        self.bus = bus
        self.normalise = normalise or plate_normaliser_for(cfg)
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.linger = linger
        self.max_failures_kept = max_failures_kept
        self.events_written = 0
        self.batches = 0
        # The first `max_failures_kept` errors, verbatim; `failure_total` is the count.
        self.failures: list[tuple[str, str]] = []
        self.failure_total = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Seed the configuration rows, then start draining.

        Seeding happens on the calling thread so a missing calibration file is
        reported before inference begins, not from inside a worker nobody is
        watching.
        """
        sync_configuration(self.database, self.cfg, normalise=self.normalise)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="persistence", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> dict:
        """Stop the thread, then write everything still on the bus.

        Draining after the join, in this thread, is what keeps a run's last
        session from being lost to the shutdown: the one way this component could
        drop data would be exiting with events queued, and it does not.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.error("persistence thread still running after %.1fs", timeout)
        while True:
            events = self.bus.drain(0.0)
            if not events:
                break
            for start in range(0, len(events), self.batch_size):
                self._write(events[start : start + self.batch_size])
        return self.stats()

    def stats(self) -> dict:
        return {
            "events_written": self.events_written,
            "batches": self.batches,
            "failures": self.failure_total,
            "dropped_by_bus": getattr(self.bus, "dropped", 0),
        }

    # -- internals ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            # `drain` blocks up to a poll interval for the first event, so an idle
            # database costs no CPU.
            events = self.bus.drain(self.poll_interval)
            if not events:
                continue
            # A lot camera publishes a SUMMARY every frame, so taking only what is
            # queued means one transaction per frame: the commit costs more than
            # the rows do. Waiting a moment for a fuller batch is what makes the
            # per-frame events nearly free - and it is bounded by `linger`, so a
            # quiet camera is still written within a tenth of a second.
            deadline = time.monotonic() + self.linger
            while len(events) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                more = self.bus.drain(remaining)
                if not more:
                    break
                events.extend(more)
            for start in range(0, len(events), self.batch_size):
                self._write(events[start : start + self.batch_size])

    def _write(self, events: list[Event]) -> None:
        if not events:
            return
        self.batches += 1
        room = len(self.failures) < self.max_failures_kept
        try:
            self.events_written += apply_events(self.database, events, normalise=self.normalise)
            return
        except Exception as exc:
            if room:
                log.error("batch of %d events rejected (%s); retrying one by one", len(events), exc)
        for event in events:
            try:
                self.events_written += apply_events(self.database, [event], normalise=self.normalise)
            except Exception as exc:
                # Counted always, kept verbatim only while the sample has room.
                # The count is never capped, so this is not swallowing: an API
                # that runs a shift and meets one poison event every frame would
                # otherwise grow a list and a traceback log without limit.
                self.failure_total += 1
                if len(self.failures) >= self.max_failures_kept:
                    continue
                self.failures.append((event.type, f"{type(exc).__name__}: {exc}"))
                log.exception("event %s from %s could not be persisted", event.type, event.camera_id)
                if len(self.failures) == self.max_failures_kept:
                    log.error(
                        "failure sample is full at %d: further failures are counted, not kept",
                        self.max_failures_kept,
                    )


def format_failures(
    failures: list[tuple[str, str]],
    limit: int = 5,
    *,
    total: int | None = None,
) -> str:
    """The CLI's summary line: how many, and the first few verbatim.

    `total` is the worker's count, which outlives the capped sample; without it
    the list's own length is the count.
    """
    count = len(failures) if total is None else max(total, len(failures))
    if count == 0:
        return "no persistence failures"
    shown = failures[:limit]
    head = "; ".join(f"{etype}: {error}" for etype, error in shown)
    more = "" if count <= len(shown) else f" (+{count - len(shown)} more)"
    return f"{count} event(s) could not be persisted: {head}{more}"
