"""ANPR pipeline for a gate camera (Phase 5).

    vehicle track (existing Tracker)
      -> plate candidates (PlateDetector, geometry screens)
      -> plate crop (PlateReader)
      -> preprocess -> OCR -> validate
      -> per-track multi-frame vote (TrackPlateRegistry)
      -> stable PlateResult, published as a `plate_read` event

It deliberately does not touch occupancy: no slots, no FSM, no billing, no
sessions. Phases 1-4 own the lot camera; this owns the entrance camera.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from smart_parking.anpr.association import TrackPlateRegistry
from smart_parking.anpr.engine import build_ocr
from smart_parking.anpr.plate_detector import PlateDetector
from smart_parking.anpr.reader import PlateReader
from smart_parking.anpr.validation import PlateValidator
from smart_parking.core.config import AppConfig
from smart_parking.core.events import EventBus
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import Event, PLATE_READ, PlateObservation, PlateResult, Track

log = get_logger("anpr.pipeline")


@dataclass
class AnprStats:
    vehicles: int = 0
    candidates: int = 0
    reads: int = 0
    accepted_reads: int = 0
    repaired_reads: int = 0
    stable_results: int = 0
    detect_seconds: float = 0.0
    ocr_seconds: float = 0.0

    @property
    def read_accept_rate(self) -> float:
        return self.accepted_reads / self.reads if self.reads else 0.0


class AnprPipeline:
    def __init__(
        self,
        cfg: AppConfig,
        camera_id: str,
        bus: EventBus | None = None,
        ocr=None,
    ) -> None:
        self.cfg = cfg
        self.camera_id = camera_id
        self.bus = bus
        self.anpr = cfg.anpr
        # `or` would swallow an empty bus: InMemoryEventBus defines __len__.
        self._ocr = ocr if ocr is not None else build_ocr(cfg.anpr)
        self.validator = PlateValidator(cfg.anpr.validation)
        self.detector = PlateDetector(cfg.anpr.detector, self._ocr)
        self.reader = PlateReader(cfg.anpr.ocr, self._ocr, self.validator)
        self.registry = TrackPlateRegistry(cfg.anpr.voting)
        self.stats = AnprStats()
        # Track ids already reported at their current plate, so a stable read
        # publishes once instead of on every frame.
        self._reported: dict[int, str] = {}

    def process(self, frame: np.ndarray, tracks: list[Track], now: float) -> list[PlateResult]:
        """One frame in, any newly stable plates out."""
        if not self.anpr.enabled:
            return []

        # A window that expired belongs to a vehicle that has left the frame, so
        # its "already reported" mark must go with it: otherwise the id is
        # silently claimed forever and a genuinely new vehicle behind it is
        # never reported.
        for track_id in self.registry.prune(now):
            self._reported.pop(track_id, None)
        results: list[PlateResult] = []

        for track in tracks:
            self.stats.vehicles += 1
            started = time.perf_counter()
            candidates = self.detector.detect(frame, track, now)
            self.stats.detect_seconds += time.perf_counter() - started
            self.stats.candidates += len(candidates)

            for detection in candidates:
                started = time.perf_counter()
                observation = self.reader.read(frame, detection)
                self.stats.ocr_seconds += time.perf_counter() - started
                self.stats.reads += 1
                if observation.repaired:
                    self.stats.repaired_reads += 1
                if not observation.accepted:
                    log.debug(
                        "track %d rejected %r (%.2f)",
                        track.track_id, observation.raw_text, observation.ocr_confidence,
                    )
                    continue
                self.stats.accepted_reads += 1
                result = self._vote(observation, track)
                if result is not None:
                    results.append(result)

        return results

    def _vote(self, observation: PlateObservation, track: Track) -> PlateResult | None:
        vote = self.registry.record(observation)
        if vote is None or not vote.stable:
            return None
        result = self.registry.result_for(
            observation.track_id, vote, class_name=track.class_name
        )
        if result is None:
            return None
        if self._reported.get(result.track_id) == result.plate_text:
            return None
        self._reported[result.track_id] = result.plate_text
        self.stats.stable_results += 1
        log.info(
            "track %d plate %s (%d/%d reads agree %.0f%%, ocr %.2f, det %.2f)",
            result.track_id,
            result.plate_text,
            vote.count,
            vote.total,
            vote.agreement * 100,
            vote.mean_score,
            observation.detector_confidence,
        )
        self._publish(result)
        return result

    def _publish(self, result: PlateResult) -> None:
        if self.bus is None:
            return
        self.bus.publish(
            Event(
                type=PLATE_READ,
                camera_id=self.camera_id,
                timestamp=result.timestamp,
                payload=result.to_dict(),
            )
        )

    def plate_for(self, track_id: int) -> PlateResult | None:
        """Current best for a track, stable or not - for overlay drawing."""
        vote = self.registry.vote(track_id)
        if vote is None:
            return None
        return self.registry.result_for(track_id, vote)

    def reset(self) -> None:
        """Call on camera reconnect: stale windows must not cross a gap."""
        self.registry.clear()
        self._reported.clear()
