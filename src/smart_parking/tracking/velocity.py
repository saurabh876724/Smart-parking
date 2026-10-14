"""Track velocity gating (architecture doc, Phase 4).

A car cruising the aisle overlaps bays for a frame or two at a time, and
containment alone cannot tell that from parking. So the eligibility question -
"is this track slow enough to be parking?" - is answered here, before slot
assignment. The FSM is untouched: gating only decides which tracks may
contribute evidence.

Velocity is the least-squares slope of a short rolling window of ground-contact
points, in **pixels per second at the source resolution**. The caller supplies
every timestamp (video time for replay, wall-clock for live), so this module
never reads a clock itself - that is what makes a 3x-speed replay produce the
same physical estimate as normal-speed processing.

Pixel velocity is not vehicle speed: the same 30 km/h looks very different
depending on camera height, lens and how far down the frame the car is. The
threshold is therefore per-camera and must be re-measured if the view changes.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from smart_parking.core.config import VelocityGatingConfig
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import Track

log = get_logger("tracking.velocity")


@dataclass(frozen=True)
class _Sample:
    t: float
    x: float
    y: float


class VelocityGate:
    """Filters a frame's tracks down to the ones allowed to claim a slot."""

    def __init__(self, cfg: VelocityGatingConfig) -> None:
        self.cfg = cfg
        self._history: dict[int, deque[_Sample]] = {}
        self._window = max(2, cfg.smoothing_window)

    @staticmethod
    def ground_point(track: Track) -> tuple[float, float]:
        """Midpoint of the footprint's bottom edge - where the tyres are.

        The box centre drifts with pitch/roll of the vehicle and, in an angled
        view, the roof belongs to a different part of the lot than the wheels.
        """
        x1, _, x2, y2 = track.bbox
        return ((x1 + x2) / 2.0, y2)

    def update(self, tracks: list[Track], now: float) -> list[Track]:
        """Record this frame's positions and return the eligible tracks."""
        if not self.cfg.enabled:
            return list(tracks)

        self._prune(now)
        eligible: list[Track] = []
        for track in tracks:
            x, y = self.ground_point(track)
            history = self._history.setdefault(
                track.track_id, deque(maxlen=self._window)
            )
            history.append(_Sample(now, x, y))

            speed = self.speed(track.track_id)
            if speed is None or speed <= self.cfg.max_parking_velocity:
                eligible.append(track)
            else:
                log.debug(
                    "track %d gated out: %.1f px/s > %.1f",
                    track.track_id,
                    speed,
                    self.cfg.max_parking_velocity,
                )
        return eligible

    def speed(self, track_id: int) -> float | None:
        """Windowed speed in px/s, or None while there is not enough history.

        Least squares over the window rather than the last two samples: single
        frame-to-frame displacement on a parked car is pure detector jitter, and
        fitting a line averages that jitter out instead of amplifying it.
        """
        history = self._history.get(track_id)
        if not history or len(history) < 2:
            return None

        n = len(history)
        t_mean = sum(s.t for s in history) / n
        x_mean = sum(s.x for s in history) / n
        y_mean = sum(s.y for s in history) / n
        denom = sum((s.t - t_mean) ** 2 for s in history)
        if denom <= 0.0:  # every sample shares one timestamp
            return None

        vx = sum((s.t - t_mean) * (s.x - x_mean) for s in history) / denom
        vy = sum((s.t - t_mean) * (s.y - y_mean) for s in history) / denom
        return math.hypot(vx, vy)

    def _prune(self, now: float) -> None:
        """Drop tracks we have not seen for a while.

        A track that vanished and came back - an occlusion, or a camera
        reconnecting - starts a fresh window instead of averaging its
        displacement across the gap, which would read as a huge velocity (or
        hide a real one) depending on which way it jumped.
        """
        cutoff = now - self.cfg.max_sample_gap_s
        stale = [tid for tid, h in self._history.items() if not h or h[-1].t < cutoff]
        for track_id in stale:
            del self._history[track_id]
