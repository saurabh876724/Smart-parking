"""Multi-frame plate voting (Phase 5).

A single OCR read is a guess; a repeated one is a measurement. This module owns
the consensus rule and nothing else - it takes strings and scores, so it can be
reused for any repeated-observation problem and so the OCR engine stays free of
policy. It has no image, torch or onnxruntime imports on purpose, which is also
what makes it cheap to test.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from smart_parking.core.config import AnprVotingConfig
from smart_parking.core.logging import get_logger

log = get_logger("anpr.voting")


@dataclass(frozen=True)
class Vote:
    """The leading plate for one track and how much of the window backs it.

    `stable` is decided by :class:`PlateVoter` against the configured floor, so
    the vote itself is plain data a caller can log or compare.
    """

    plate: str
    count: int
    total: int          # accepted reads in the window
    agreement: float    # count / total
    mean_score: float   # mean OCR confidence over the agreeing reads
    last_seen: float
    stable: bool = False


@dataclass(frozen=True)
class _Read:
    plate: str
    score: float
    timestamp: float


class PlateVoter:
    """Rolling-window majority for one vehicle.

    Reads that failed validation never enter the window: an unreadable frame
    carries no opinion about the plate, so it must not dilute the agreement of
    the frames that did read it. Volume is still controlled by
    `minimum_observations`, so a track with two lucky reads never confirms.
    """

    def __init__(self, cfg: AnprVotingConfig) -> None:
        self.cfg = cfg
        self._reads: deque[_Read] = deque(maxlen=cfg.window_size)
        self.last_seen = 0.0

    def add(self, plate: str, score: float, timestamp: float) -> Vote:
        self._reads.append(_Read(plate, score, timestamp))
        self.last_seen = timestamp
        return self.current()

    def current(self) -> Vote | None:
        if not self._reads:
            return None

        grouped: dict[str, list[_Read]] = {}
        for read in self._reads:
            grouped.setdefault(read.plate, []).append(read)

        # Ties break on mean OCR confidence, then on recency, so the result is
        # reproducible instead of dict-order dependent.
        def rank(item: tuple[str, list[_Read]]):
            reads = item[1]
            mean = sum(r.score for r in reads) / len(reads)
            latest = max(r.timestamp for r in reads)
            return (len(reads), mean, latest)

        plate, reads = max(grouped.items(), key=rank)
        total = len(self._reads)
        mean_score = sum(r.score for r in reads) / len(reads)
        count = len(reads)
        vote = Vote(
            plate=plate,
            count=count,
            total=total,
            agreement=count / total,
            mean_score=mean_score,
            last_seen=max(r.timestamp for r in reads),
            # `count`, not `agreement`, is the primary gate: one clean read of a
            # crisp plate is 100% agreement and still not enough to open a gate.
            stable=(
                count >= self.cfg.minimum_observations
                and count / total >= self.cfg.minimum_agreement
            ),
        )
        if vote.stable and count < total:
            log.debug(
                "plate %s stable with %d/%d reads (%.0f%%)",
                plate, count, total, vote.agreement * 100,
            )
        return vote

    def __len__(self) -> int:
        return len(self._reads)
