"""Track-to-plate association (Phase 5).

Keeps one :class:`PlateVoter` per tracker id, so a vehicle's reads accumulate on
that vehicle and nowhere else. Windows expire: a track that has not been seen
for `voting.max_track_age_s` is dropped outright, which is what stops a reused
ByteTrack id from inheriting the previous car's plate.
"""

from __future__ import annotations

from smart_parking.anpr.voting import PlateVoter, Vote
from smart_parking.core.config import AnprVotingConfig
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import PlateObservation, PlateResult

log = get_logger("anpr.association")


class TrackPlateRegistry:
    def __init__(self, cfg: AnprVotingConfig) -> None:
        self.cfg = cfg
        self._voters: dict[int, PlateVoter] = {}
        self._last: dict[int, PlateObservation] = {}

    def record(self, observation: PlateObservation) -> Vote | None:
        """Add one accepted read and return the track's current vote."""
        if observation.plate is None:
            return None
        voter = self._voters.setdefault(observation.track_id, PlateVoter(self.cfg))
        self._last[observation.track_id] = observation
        return voter.add(observation.plate, observation.ocr_confidence, observation.timestamp)

    def vote(self, track_id: int) -> Vote | None:
        voter = self._voters.get(track_id)
        return voter.current() if voter else None

    def prune(self, now: float) -> list[int]:
        """Drop tracks whose window has gone stale. Returns the ids removed."""
        cutoff = now - self.cfg.max_track_age_s
        expired = [tid for tid, voter in self._voters.items() if voter.last_seen < cutoff]
        for track_id in expired:
            del self._voters[track_id]
            self._last.pop(track_id, None)
        if expired:
            log.debug("expired %d stale track window(s): %s", len(expired), expired)
        return expired

    def result_for(self, track_id: int, vote: Vote, *, class_name: str = "") -> PlateResult | None:
        """Build the reported result for a stable vote.

        The geometry and detector score come from the most recent observation of
        that track - the vote itself only carries text and OCR scores.
        """
        observation = self._last.get(track_id)
        if observation is None:
            return None
        return PlateResult(
            track_id=track_id,
            plate_text=vote.plate,
            plate_confidence=observation.detector_confidence,
            ocr_confidence=vote.mean_score,
            timestamp=vote.last_seen,
            bbox=observation.bbox,
            stable=True,
            observations=vote.count,
            agreement=vote.agreement,
            class_name=class_name,
        )

    def clear(self) -> None:
        self._voters.clear()
        self._last.clear()

    def __len__(self) -> int:
        return len(self._voters)
