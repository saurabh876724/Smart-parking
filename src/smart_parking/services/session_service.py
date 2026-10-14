"""Parking sessions (Phase 6): gate crossings + plates become visit records.

The gate camera says "a vehicle crossed in at t1, track 12"; the ANPR stage
says "track 12 is MH-14-AB-1234, 0.94"; this module joins those two facts and
times the visit. It is the first module that lives in ``services/`` because it
is the seam the doc describes: it consumes the AI side's events and produces
the business side's records.

In memory only - no database, no API, no billing. The state machine is the part
worth getting right first, because it is testable with hand-written crossings:
entry, late plate, exit by line or by dwell, and the two anomalies the doc
calls out (a second ACTIVE session for one plate, an exit with no entry).

``status`` is the lifecycle and ``review_reason`` / ``anomaly_reason`` are the
flags, kept separate on purpose. A session that has been entered but not yet
exited is ACTIVE whether or not its plate is legible; the operator's "look at
this one" marker survives closing, which is why a session can end in
NEEDS_REVIEW rather than COMPLETED and still have a real duration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smart_parking.core.config import GateConfig
from smart_parking.core.events import EventBus
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import (
    DUPLICATE_PLATE,
    NO_ENTRY,
    UNORDERED_EXIT,
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    VIOLATION,
    Event,
    GateCrossing,
    PlateResult,
    SessionStatus,
)
from smart_parking.parking.gate_logic import EXIT, ENTRY

log = get_logger("services.sessions")


@dataclass
class ParkingSession:
    """One visit: a vehicle that came in through a gate line, and (eventually)
    left again."""

    session_id: str
    camera_id: str
    track_id: int
    entry_at: float | None
    plate: str | None = None
    ocr_confidence: float | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    exit_at: float | None = None
    # "line" - it crossed the exit line; "dwell" - it vanished from the exit
    # zone, which is the usual way a barrier exit looks on camera.
    exit_reason: str | None = None
    slot_id: str | None = None
    review_reason: str | None = None
    anomaly_reason: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.camera_id, self.track_id)

    @property
    def closed(self) -> bool:
        return self.exit_at is not None

    @property
    def duration_s(self) -> float | None:
        """Wall-clock-free: entry and exit are both video time from the camera
        that saw them, so a dropped frame does not stretch the visit."""
        if self.entry_at is None or self.exit_at is None:
            return None
        return self.exit_at - self.entry_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "camera_id": self.camera_id,
            "track_id": self.track_id,
            "plate": self.plate,
            "status": self.status.value,
            "entry_ts": None if self.entry_at is None else round(self.entry_at, 3),
            "exit_ts": None if self.exit_at is None else round(self.exit_at, 3),
            "duration": None if self.duration_s is None else round(self.duration_s, 3),
            "exit_reason": self.exit_reason,
            "slot_id": self.slot_id,
            "ocr_confidence": None if self.ocr_confidence is None else round(self.ocr_confidence, 3),
            "review_reason": self.review_reason,
            "anomaly_reason": self.anomaly_reason,
        }


class SessionService:
    def __init__(self, cfg: GateConfig, bus: EventBus | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        # Every session ever produced this run, keyed by id, in creation order.
        self.sessions: dict[str, ParkingSession] = {}
        # Sessions still waiting for their exit, keyed by (camera, track).
        self._open: dict[tuple[str, int], ParkingSession] = {}
        # A plate read before the vehicle reached the line - normal, since the
        # plate is legible a second or two before the centroid crosses it. Held
        # until either the entry consumes it or the track goes stale.
        self._pending_plates: dict[tuple[str, int], PlateResult] = {}
        self._seq = 0
        # Diagnostics, not behaviour: how many duplicate crossings arrived.
        self.duplicate_crossings = 0

    # -- crossings ----------------------------------------------------------
    def crossing(self, cross: GateCrossing) -> list[Event]:
        """Apply one gate crossing. Returns the events it produced."""
        if cross.role == ENTRY:
            return self._enter(cross)
        if cross.role == EXIT:
            return self._exit(cross)
        log.warning("unknown gate role %r ignored", cross.role)
        return []

    def _enter(self, cross: GateCrossing) -> list[Event]:
        key = (cross.camera_id, cross.track_id)
        existing = self._open.get(key)
        if existing is not None:
            # Same vehicle, same line, second report: never a second session.
            self.duplicate_crossings += 1
            log.debug("duplicate entry for track %s not re-sessioned", cross.track_id)
            return []

        session = self._new(cross.camera_id, cross.track_id, cross.timestamp)
        pending = self._pending_plates.pop(key, None)
        if pending is not None:
            self._bind(session, pending)

        self._open[key] = session
        if self._flag_duplicate(session):
            return self._publish(
                VIOLATION, session, timestamp=cross.timestamp, kind=DUPLICATE_PLATE
            )
        return self._publish(VEHICLE_ENTERED, session, timestamp=cross.timestamp)

    def _exit(self, cross: GateCrossing) -> list[Event]:
        key = (cross.camera_id, cross.track_id)
        session = self._open.pop(key, None)
        if session is None:
            # An exit with no entry: the vehicle reversed in through the boom,
            # or tracking lost its id on the way in. There is no honest duration
            # to report, so this is an anomaly record plus a violation event -
            # not a VEHICLE_EXITED, which downstream means "has a duration".
            anomaly = self._new(cross.camera_id, cross.track_id, None)
            anomaly.exit_at = cross.timestamp
            anomaly.exit_reason = "dwell" if cross.direction == "dwell" else "line"
            anomaly.status = SessionStatus.ANOMALY
            anomaly.anomaly_reason = NO_ENTRY
            pending = self._pending_plates.pop(key, None)
            if pending is not None:
                self._bind(anomaly, pending)
            return self._publish(
                VIOLATION, anomaly, timestamp=cross.timestamp, kind=NO_ENTRY
            )

        if session.entry_at is not None and cross.timestamp < session.entry_at:
            # A dwell exit is stamped at the vehicle's last sighting in the zone,
            # so it can arrive dated *before* the entry this track id opened -
            # which is the documented cost of keying a visit on a ByteTrack id
            # (see the gate limitations). Two timestamps that cannot describe one
            # visit are not a visit: the entry stays, the exit is not stamped, and
            # there is no duration. Not a VEHICLE_EXITED either, which downstream
            # means "has a duration"; the event log keeps the claimed time.
            session.status = SessionStatus.ANOMALY
            session.anomaly_reason = UNORDERED_EXIT
            log.warning(
                "session %s exit at %.3f precedes its entry at %.3f - anomaly, no duration",
                session.session_id, cross.timestamp, session.entry_at,
            )
            return self._publish(
                VIOLATION,
                session,
                timestamp=cross.timestamp,
                kind=UNORDERED_EXIT,
                claimed_exit_ts=round(cross.timestamp, 3),
                claimed_exit_reason="dwell" if cross.direction == "dwell" else "line",
            )

        session.exit_at = cross.timestamp
        session.exit_reason = "dwell" if cross.direction == "dwell" else "line"
        if session.plate is None:
            session.review_reason = session.review_reason or "no plate read"
        if session.anomaly_reason is None:
            session.status = (
                SessionStatus.NEEDS_REVIEW
                if session.review_reason
                else SessionStatus.COMPLETED
            )
        log.info(
            "session %s exit (%s) duration %.1fs plate %s",
            session.session_id, session.exit_reason,
            session.duration_s or 0.0, session.plate or "-",
        )
        return self._publish(VEHICLE_EXITED, session, timestamp=cross.timestamp)

    # -- plates -------------------------------------------------------------
    def plate(self, result: PlateResult, camera_id: str) -> ParkingSession | None:
        """Bind a stable plate to the session for that track.

        Returns the session it landed on, or None when it was parked as pending
        because the vehicle has not crossed the entry line yet.
        """
        key = (camera_id, result.track_id)
        session = self._open.get(key)
        if session is None:
            self._pending_plates[key] = result
            return None
        self._bind(session, result)
        # The plate is usually read after the vehicle has already crossed in,
        # so this is where a second visit by the same number shows up.
        if self._flag_duplicate(session):
            self._publish(VIOLATION, session, timestamp=result.timestamp, kind=DUPLICATE_PLATE)
        return session

    def _flag_duplicate(self, session: ParkingSession) -> bool:
        """Two live sessions claiming one plate: flag the newcomer.

        Neither one is dropped. The doc's rule is that a duplicate ACTIVE
        session for a plate is an anomaly, and picking a winner here would be a
        guess - both records stay, both close with their own durations, and the
        operator decides which visit is real.
        """
        if session.plate is None or session.anomaly_reason is not None:
            return False
        clash = self.active_for_plate(session.plate, exclude=session)
        if clash is None:
            return False
        session.anomaly_reason = DUPLICATE_PLATE
        session.status = SessionStatus.ANOMALY
        log.warning(
            "plate %s already has open session %s - marking %s as anomaly",
            session.plate, clash.session_id, session.session_id,
        )
        return True

    def _bind(self, session: ParkingSession, result: PlateResult) -> None:
        if session.plate is not None and session.plate != result.plate_text:
            # A corrected or re-voted plate. Trust the newest stable read, but
            # say so: an operator comparing two reports needs the reason.
            session.review_reason = session.review_reason or "plate changed"
        session.plate = result.plate_text
        session.ocr_confidence = result.ocr_confidence
        if result.ocr_confidence < self.cfg.min_ocr_confidence:
            session.review_reason = (
                session.review_reason
                or f"plate confidence {result.ocr_confidence:.2f} below "
                f"{self.cfg.min_ocr_confidence:.2f}"
            )

    # -- queries ------------------------------------------------------------
    def active_for_plate(self, plate: str, exclude: ParkingSession | None = None) -> ParkingSession | None:
        for session in self.sessions.values():
            if session is exclude or session.closed or session.plate != plate:
                continue
            if session.anomaly_reason is None:
                return session
        return None

    def open_sessions(self) -> list[ParkingSession]:
        return [s for s in self.sessions.values() if not s.closed]

    def bind_slot(self, session_id: str, slot_id: str) -> ParkingSession | None:
        """Say which slot this visit used.

        Nothing calls this yet, and that is deliberate: the lot camera reports a
        *lot* track id and the gate camera reports a *gate* track id, and tying
        them together needs either one camera covering both or cross-camera
        plate matching (doc Phase 10). Until something can answer the question
        honestly the session's ``slot_id`` stays None, which the doc explicitly
        allows - a wrong slot is worse than an empty one.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return None
        session.slot_id = slot_id
        return session

    # -- lifecycle ----------------------------------------------------------
    def prune(self, now: float) -> None:
        """Drop pending plates whose vehicle never reached the line.

        The TTL is the gate's own track timeout: past that the tracker is
        expected to have forgotten the id, so the plate could not bind to a
        session anyway, and keeping it risks putting it on the next car.
        """
        cutoff = now - self.cfg.track_timeout_s
        for key in [k for k, r in self._pending_plates.items() if r.timestamp < cutoff]:
            del self._pending_plates[key]

    def reset(self) -> None:
        """Call on camera reconnect - a fresh stream has no history."""
        self.sessions.clear()
        self._open.clear()
        self._pending_plates.clear()
        self.duplicate_crossings = 0
        self._seq = 0

    # -- internals ----------------------------------------------------------
    def _new(self, camera_id: str, track_id: int, entry_at: float | None) -> ParkingSession:
        self._seq += 1
        session = ParkingSession(
            session_id=f"{camera_id}-{self._seq:05d}",
            camera_id=camera_id,
            track_id=track_id,
            entry_at=entry_at,
        )
        self.sessions[session.session_id] = session
        return session

    def _publish(
        self, etype: str, session: ParkingSession, *, timestamp: float, **extra: Any
    ) -> list[Event]:
        if self.bus is None:
            return []
        event = Event(
            type=etype,
            camera_id=session.camera_id,
            timestamp=timestamp,
            payload={**session.to_dict(), **extra},
        )
        self.bus.publish(event)
        return [event]
