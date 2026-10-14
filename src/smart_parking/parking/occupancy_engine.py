"""Track -> slot assignment and slot state machines.

Perception emits facts ("track 7 covers 62% of slot A3"); this module turns
them into debounced slot states and events. It has no database, no API and no
frame access, so it can be unit-tested with scripted sequences.
"""

from __future__ import annotations

from smart_parking.core.config import OccupancyConfig
from smart_parking.core.events import EventBus
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import (
    SLOT_FREED,
    SLOT_OCCUPIED,
    SLOT_SNAPSHOT,
    SUMMARY,
    VIOLATION,
    Event,
    Polygon,
    SlotState,
    SlotStatus,
    Track,
)
from smart_parking.parking.geometry import containment_score, overlap_score, to_pixels
from smart_parking.parking.slot_fsm import SlotEvidence, SlotFSM
from smart_parking.tracking.velocity import VelocityGate

log = get_logger("parking.occupancy")


class OccupancyEngine:
    def __init__(
        self,
        camera_id: str,
        slots: dict[str, Polygon],
        cfg: OccupancyConfig,
        bus: EventBus | None = None,
        identity_stable: bool = True,
        gate: VelocityGate | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.slots = slots
        self.cfg = cfg
        self.bus = bus
        # None when tracking is off: synthetic per-frame ids carry no motion
        # information, so there is nothing honest to gate on.
        self.gate = gate
        self.fsms: dict[str, SlotFSM] = {
            sid: SlotFSM(sid, cfg, identity_stable=identity_stable) for sid in slots
        }
        self.straddling: dict[int, list[tuple[str, float]]] = {}
        self.last_evidence: dict[str, SlotEvidence] = {}
        # Whether the first reading of every bay has been announced yet.
        self.snapshot_published = False
        # Slots arrive normalised (0-1) so calibration survives a resolution
        # change, but footprints are in pixels. Convert once per size.
        self._pixel_slots: dict[tuple[int, int], dict[str, Polygon]] = {}

    def pixel_slots(self, width: int, height: int) -> dict[str, Polygon]:
        cached = self._pixel_slots.get((width, height))
        if cached is None:
            cached = {sid: to_pixels(poly, width, height) for sid, poly in self.slots.items()}
            self._pixel_slots[(width, height)] = cached
        return cached

    # -- main entry point --------------------------------------------------
    def update(self, tracks: list[Track], now: float, frame_size: tuple[int, int]) -> list[SlotState]:
        width, height = frame_size
        # Gating is eligibility, not assignment: a track moving too fast to be
        # parking simply contributes no evidence this frame, which lets the FSM's
        # own timers decide what that means.
        eligible = self.gate.update(tracks, now) if self.gate is not None else tracks
        return self._apply(self._assign(eligible, self.pixel_slots(width, height)), now)

    def tick(self, now: float) -> list[SlotState]:
        """Advance the dwell timers on a frame we skipped inference for.

        Re-uses the previous evidence. Feeding an empty track list here would
        tell every FSM "no vehicle visible", which resets the pending timers -
        that is how frame-skipping silently prevented any slot from ever
        reaching OCCUPIED.
        """
        return self._apply(self.last_evidence, now)

    def _apply(self, evidence: dict[str, SlotEvidence], now: float) -> list[SlotState]:
        self.last_evidence = evidence
        changed: list[tuple[str, SlotStatus]] = []

        for slot_id, fsm in self.fsms.items():
            new_status = fsm.update(evidence.get(slot_id), now)
            if new_status is not None:
                changed.append((slot_id, new_status))

        # A bay that stays empty never produces a transition, so the transition
        # events alone would leave its row at the UNKNOWN it was created with.
        # One announcement per bay, made after real evidence has been fed, is
        # what lets UNKNOWN keep meaning "the camera is not telling us".
        if not self.snapshot_published:
            self.snapshot_published = True
            self._emit_snapshot(now, skip={slot_id for slot_id, _ in changed})

        self._emit(changed, evidence, now)
        return [self.state_of(sid) for sid in self.fsms]

    def _assign(
        self, tracks: list[Track], slots: dict[str, Polygon]
    ) -> dict[str, SlotEvidence]:
        """Give each vehicle at most one slot, greedily by best containment.

        Callers pass only tracks that are eligible to park (see ``update``);
        the geometry here is unchanged from Phase 3.
        """
        candidates: list[tuple[float, int, str, float]] = []
        for tr in tracks:
            for slot_id, poly in slots.items():
                fit = containment_score(tr.footprint, poly)
                if fit >= self.cfg.any_slot_score:
                    candidates.append((fit, tr.track_id, slot_id, overlap_score(tr.footprint, poly)))
        candidates.sort(reverse=True)

        taken_slots: dict[str, SlotEvidence] = {}
        claimed_tracks: set[int] = set()
        # Per track, every slot it covers at a meaningful rate. Two entries
        # means the vehicle is sitting across a line, whichever one "wins".
        straddling: dict[int, list[tuple[str, float]]] = {}

        for fit, track_id, slot_id, coverage in candidates:
            if fit >= self.cfg.occupy_score and coverage >= self.cfg.straddle_score:
                straddling.setdefault(track_id, []).append((slot_id, coverage))
            if track_id in claimed_tracks or slot_id in taken_slots:
                continue
            taken_slots[slot_id] = SlotEvidence(track_id=track_id, score=fit)
            claimed_tracks.add(track_id)

        self.straddling = straddling
        return taken_slots

    # -- outputs -----------------------------------------------------------
    def state_of(self, slot_id: str) -> SlotState:
        fsm = self.fsms[slot_id]
        return SlotState(
            slot_id=slot_id,
            status=fsm.status,
            since=fsm.since,
            score=fsm.score,
            track_id=fsm.track_id,
        )

    def summary(self) -> dict[str, int]:
        counts = {s.value: 0 for s in SlotStatus}
        for fsm in self.fsms.values():
            counts[fsm.status.value] += 1
        occupied = counts[SlotStatus.OCCUPIED.value] + counts[SlotStatus.PENDING_FREE.value]
        available = counts[SlotStatus.FREE.value] + counts[SlotStatus.PENDING_OCCUPIED.value]
        return {
            "total": len(self.fsms),
            "occupied": occupied,
            "available": available,
            "unknown": counts[SlotStatus.UNKNOWN.value],
        }

    def _emit_snapshot(self, now: float, *, skip: set[str] = frozenset()) -> None:
        """Publish this engine's first reading of each bay it watches.

        ``skip`` holds bays that already announced themselves through a real
        transition on this same frame, so a bay is never reported twice.
        """
        if self.bus is None:
            return
        for slot_id, fsm in self.fsms.items():
            if slot_id in skip:
                continue
            self.bus.publish(
                Event(
                    type=SLOT_SNAPSHOT,
                    camera_id=self.camera_id,
                    timestamp=now,
                    payload={
                        "slot_id": slot_id,
                        "status": fsm.committed.value,
                        "track_id": fsm.track_id,
                        "score": round(fsm.score, 3),
                    },
                )
            )

    def _emit(
        self,
        changed: list[tuple[str, SlotStatus]],
        evidence: dict[str, SlotEvidence],
        now: float,
    ) -> None:
        if self.bus is None:
            return
        for slot_id, status in changed:
            fsm = self.fsms[slot_id]
            etype = SLOT_OCCUPIED if status is SlotStatus.OCCUPIED else SLOT_FREED
            self.bus.publish(
                Event(
                    type=etype,
                    camera_id=self.camera_id,
                    timestamp=now,
                    payload={
                        "slot_id": slot_id,
                        "status": status.value,
                        "track_id": fsm.track_id,
                        "score": round(fsm.score, 3),
                    },
                )
            )
            log.info("slot %s -> %s (track=%s)", slot_id, status.value, fsm.track_id)

        for track_id, entries in self.straddling.items():
            if len(entries) < 2:
                continue
            self.bus.publish(
                Event(
                    type=VIOLATION,
                    camera_id=self.camera_id,
                    timestamp=now,
                    payload={
                        "kind": "OUTSIDE_SLOT" if entries[0][1] < 0.3 else "DOUBLE_PARK",
                        "track_id": track_id,
                        "slots": [s for s, _ in entries],
                        "severity": "MEDIUM",
                    },
                )
            )

        self.bus.publish(
            Event(type=SUMMARY, camera_id=self.camera_id, timestamp=now, payload=self.summary())
        )

    def mark_all(self, status: SlotStatus, now: float) -> None:
        """Camera went offline: slots become UNKNOWN, never FREE."""
        for fsm in self.fsms.values():
            if fsm.force(status, now):
                log.warning("slot %s forced to %s", fsm.slot_id, status.value)
