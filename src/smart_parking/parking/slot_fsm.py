"""Per-slot debounce state machine (architecture doc, figure 2).

Raw overlap evidence is never trusted for a single frame. A slot only changes
state after the evidence has persisted for a configured dwell time, and the
timers are deliberately asymmetric: a false FREE double-books a slot, while a
delayed FREE only costs a few seconds of availability.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from smart_parking.core.config import OccupancyConfig
from smart_parking.core.schemas import SlotStatus


@dataclass
class SlotEvidence:
    track_id: int
    score: float


@dataclass
class SlotFSM:
    slot_id: str
    cfg: OccupancyConfig
    status: SlotStatus = SlotStatus.FREE
    since: float = 0.0
    score: float = 0.0
    track_id: int | None = None
    # With real tracks, a new id in the same bay is a different car and must
    # re-prove its stay. Without tracking the id changes every frame, so
    # honouring it would keep resetting the dwell timer forever.
    identity_stable: bool = True
    _pending_since: float | None = None
    _pending_target: SlotStatus | None = None
    history: list[tuple[float, SlotStatus]] = field(default_factory=list)

    @property
    def committed(self) -> SlotStatus:
        """What this bay is right now, with the debounce folded away.

        A PENDING_* state is still the last committed one until the dwell timer
        proves otherwise, and it is a timer state, not a fact about the lot -
        the `parking_slots` column only ever holds a committed status.
        """
        if self.status is SlotStatus.PENDING_OCCUPIED:
            return SlotStatus.FREE
        if self.status is SlotStatus.PENDING_FREE:
            return SlotStatus.OCCUPIED
        return self.status

    def snapshot(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "status": self.status.value,
            "score": round(self.score, 3),
            "track_id": self.track_id,
            "since": self.since,
        }

    def force(self, status: SlotStatus, now: float) -> bool:
        """Used for camera-offline -> UNKNOWN and by tests."""
        if status == self.status:
            return False
        self._transition(status, now)
        return True

    def _transition(self, status: SlotStatus, now: float) -> None:
        self.status = status
        self.since = now
        self._pending_since = None
        self._pending_target = None
        self.history.append((now, status))

    def update(self, evidence: SlotEvidence | None, now: float) -> SlotStatus | None:
        """Feed one frame of evidence. Returns the new status if it changed."""
        occupied_evidence = evidence is not None and evidence.score >= self.cfg.occupy_score

        self.score = evidence.score if evidence else 0.0

        if self.status in (SlotStatus.FREE, SlotStatus.PENDING_OCCUPIED):
            if occupied_evidence:
                if self.identity_stable and self.track_id != evidence.track_id:
                    self._pending_since = now
                    self.track_id = evidence.track_id
                if self._pending_since is None:
                    self._pending_since = now
                self.status = SlotStatus.PENDING_OCCUPIED
                self._pending_target = SlotStatus.OCCUPIED
                if now - self._pending_since >= self.cfg.occupied_after_s:
                    self._transition(SlotStatus.OCCUPIED, now)
                    return SlotStatus.OCCUPIED
            else:
                # Evidence vanished before the dwell time elapsed.
                self.track_id = None
                self._pending_since = None
                self.status = SlotStatus.FREE
            return None

        if self.status in (SlotStatus.OCCUPIED, SlotStatus.PENDING_FREE):
            if occupied_evidence:
                self.track_id = evidence.track_id
                self._pending_since = None
                self.status = SlotStatus.OCCUPIED
                return None

            # A lost track simply stops providing evidence, which starts the
            # (long) free timer - that delay is the occlusion hold.
            if self._pending_since is None:
                self._pending_since = now
                self.status = SlotStatus.PENDING_FREE
                self._pending_target = SlotStatus.FREE
            if now - self._pending_since >= self.cfg.free_after_s:
                self.track_id = None
                self._transition(SlotStatus.FREE, now)
                return SlotStatus.FREE
            return None

        # UNKNOWN: only an explicit recovery call pulls a slot out of this.
        return None

    def recover(self, evidence: SlotEvidence | None, now: float) -> SlotStatus | None:
        if self.status != SlotStatus.UNKNOWN:
            return None
        self.status = SlotStatus.FREE
        self._pending_since = None
        self.track_id = None
        return self.update(evidence, now)
