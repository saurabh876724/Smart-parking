"""Gate line crossing (Phase 6).

A gate camera does not have slots; it has a line the vehicle must cross on its
way in and a line on its way out. This module turns a stream of tracked
centroids into `GateCrossing` events and nothing else - no plates, no sessions,
no database - so the crossing rules are testable against hand-written centroid
sequences.

Why the step and not the point. Testing which side of the line the centroid is
on this frame is the obvious implementation and it is wrong twice over: a car
driving fast enough can be on the far side the next frame without any frame ever
landing on the line, and a car stopped at the barrier with a 2 px centroid
wobble fires an entry every frame. So the segment from the previous centroid to
this one is tested against the line segment, and a track fires each event at
most once. That is the doc's "sign of the cross product so jitter near the line
does not double-count", with the segment test added because the sign test alone
still misses the fast car.

Direction convention (image coordinates, y downwards). `side()` below is the
cross product of the drawn line against a point. For a line drawn left to right,
a vehicle travelling *down* the frame goes from side -1 to side +1, so its
crossing direction is "positive". A gate file that has the traffic flowing the
other way says `direction: negative`, or the operator reverses the line.

Coordinate space: gate geometry is stored normalised (0-1), like the slot files,
so one drawing survives a resolution change. Track centroids arrive in pixels,
so each one is divided by the frame size before it is compared. That is exact,
not an approximation - scaling x and y independently is a linear map with
positive determinant, which preserves both the sign of a cross product and
whether two segments intersect.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from smart_parking.core.config import GateConfig
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import GateCrossing, Point, Polygon, Track
from smart_parking.parking.geometry import point_in_polygon, polygon_from_points

log = get_logger("parking.gate")

ENTRY = "ENTRY"
EXIT = "EXIT"


def side(line_a: tuple[float, float], line_b: tuple[float, float], pt: tuple[float, float]) -> float:
    """Sign of the cross product (B-A) x (P-A): which side of the line a point is on."""
    (ax, ay), (bx, by) = line_a, line_b
    px, py = pt
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax)


def segments_cross(p0: tuple[float, float], p1: tuple[float, float], a, b) -> bool:
    """Do the centroid's step and the gate line actually intersect?

    Proper crossing only: each segment must straddle the other's line. A step
    that lands exactly on the line (side == 0) counts as no crossing, which is
    what keeps a vehicle parked on the barrier from producing events.
    """
    return side(a, b, p0) * side(a, b, p1) < 0 and side(p0, p1, a) * side(p0, p1, b) < 0


@dataclass(frozen=True)
class _Line:
    name: str
    a: tuple[float, float]
    b: tuple[float, float]
    direction: str
    role: str

    def expects(self, s0: float, s1: float) -> bool:
        """Did a side change from s0 to s1 travel the configured way?"""
        forward = s0 < 0 < s1
        return forward if self.direction == "positive" else not forward


@dataclass
class _ZoneSighting:
    """A track's stay in the gate zone, kept until the dwell timer resolves it.

    Kept as a record rather than a dict entry keyed by track id because ByteTrack
    recycles ids: a pending "this vehicle may have left" record must not be
    silently overwritten by the next vehicle that inherits the number.
    """

    track_id: int
    point: tuple[float, float]  # last pixel centroid inside the zone
    in_zone_at: float  # when that sighting happened
    last_seen: float  # last time the track was seen anywhere in frame


class GateLogic:
    def __init__(self, cfg: GateConfig, camera_id: str = "cam1") -> None:
        self.cfg = cfg
        self.camera_id = camera_id
        self.lines = [
            _Line(
                name=line.name,
                a=(float(line.points[0][0]), float(line.points[0][1])),
                b=(float(line.points[1][0]), float(line.points[1][1])),
                direction=line.direction,
                role=line.role,
            )
            for line in cfg.lines
        ]
        self.zone: Polygon | None = polygon_from_points(cfg.zone) if cfg.zone else None
        # Which side of each line each track was last on, and when we saw it.
        self._prev: dict[int, tuple[float, float]] = {}
        self._seen: dict[int, float] = {}
        self._dwell: list[_ZoneSighting] = []
        # Idempotency: (camera, track, role) fires at most once.
        self._fired: set[tuple[str, int, str]] = set()
        # Diagnostics, not behaviour: how often a track crossed back.
        self.reversals: Counter[tuple[int, str]] = Counter()
        self.duplicates: Counter[tuple[int, str]] = Counter()

    def update(
        self, tracks: list[Track], now: float, frame_size: tuple[int, int]
    ) -> list[GateCrossing]:
        """Line crossings produced by this frame's centroids.

        ``frame_size`` is the size the pixels are in, used only to bring each
        centroid into the normalised space the gate file is drawn in.
        """
        out: list[GateCrossing] = []
        if not self.cfg.enabled:
            return out
        width, height = frame_size
        for track in tracks:
            pixel_point = track.centroid
            point = (pixel_point[0] / width, pixel_point[1] / height)
            track_id = track.track_id
            previous = self._prev.get(track_id)
            self._prev[track_id] = pixel_point
            self._seen[track_id] = now

            if self._in_zone(point):
                self._touch_zone(track_id, pixel_point, now)
            else:
                self._refresh_zone(track_id, now)
            if previous is None:
                continue
            previous_point = (previous[0] / width, previous[1] / height)
            if not self._step_counts(previous_point, point):
                continue

            for line in self.lines:
                s0, s1 = side(line.a, line.b, previous_point), side(line.a, line.b, point)
                if s0 * s1 >= 0 or not segments_cross(previous_point, point, line.a, line.b):
                    continue
                if not line.expects(s0, s1):
                    self.reversals[(track_id, line.name)] += 1
                    continue
                key = (self.camera_id, track_id, line.role)
                if key in self._fired:
                    # Jitter, a re-acquired id, or a car that went in and out
                    # again without the session closing: never a second event.
                    self.duplicates[(track_id, line.name)] += 1
                    log.debug("track %s crossed %s again - not re-firing", track_id, line.name)
                    continue
                self._fired.add(key)
                out.append(
                    GateCrossing(
                        camera_id=self.camera_id,
                        track_id=track_id,
                        line=line.name,
                        role=line.role,
                        timestamp=now,
                        direction=line.direction,
                        centroid=pixel_point,
                    )
                )
        return out

    def prune(self, now: float) -> list[GateCrossing]:
        """Dwell exits, and history for tracks that no longer exist.

        A vehicle that drives out of frame without crossing the exit line is the
        common case at a barrier: the boom lifts, the car leaves the camera's
        view sideways. The doc's answer is to call it an exit once it has been
        gone long enough that it is not a tracking dropout.

        The timer runs on *not being seen at all*, not on leaving the zone: a
        vehicle that is still in frame, just past the zone edge, has not left the
        gate, and calling it an exit there would close a session on a car that is
        visibly still there.
        """
        out: list[GateCrossing] = []
        if not self.cfg.enabled:
            return out
        for sighting in list(self._dwell):
            if now - sighting.last_seen < self.cfg.dwell_exit_s:
                continue
            self._dwell.remove(sighting)
            key = (self.camera_id, sighting.track_id, EXIT)
            if key in self._fired:
                continue
            self._fired.add(key)
            out.append(
                GateCrossing(
                    camera_id=self.camera_id,
                    track_id=sighting.track_id,
                    line="zone_dwell",
                    role=EXIT,
                    # The vehicle left at the last moment it was seen in the
                    # zone, not now - waiting out the timer is our uncertainty,
                    # not the parking duration.
                    timestamp=sighting.in_zone_at,
                    direction="dwell",
                    centroid=sighting.point,
                )
            )
        for track_id in [t for t, seen in self._seen.items() if now - seen > self.cfg.track_timeout_s]:
            self._prev.pop(track_id, None)
            self._seen.pop(track_id, None)
            # A dead track's "already reported" mark has to go with it, or the
            # next vehicle this id gets reused for never fires an entry.
            for role in (ENTRY, EXIT):
                self._fired.discard((self.camera_id, track_id, role))
        return out

    def reset(self) -> None:
        self._prev.clear()
        self._seen.clear()
        self._dwell.clear()
        self._fired.clear()
        self.reversals.clear()
        self.duplicates.clear()

    def _live_zone(self, track_id: int, now: float) -> _ZoneSighting | None:
        """The pending zone record for this track, if this sighting is still it.

        Past `track_timeout_s` of silence the tracker's history treats the id as
        gone, so the pending record belongs to the previous vehicle and a fresh
        one starts - otherwise a recycled id would inherit another car's last
        known position.
        """
        for sighting in reversed(self._dwell):
            if sighting.track_id != track_id:
                continue
            if now - sighting.last_seen <= self.cfg.track_timeout_s:
                return sighting
            return None
        return None

    def _touch_zone(self, track_id: int, point: tuple[float, float], now: float) -> None:
        sighting = self._live_zone(track_id, now)
        if sighting is None:
            self._dwell.append(_ZoneSighting(track_id, point, now, now))
        else:
            sighting.point = point
            sighting.in_zone_at = now
            sighting.last_seen = now

    def _refresh_zone(self, track_id: int, now: float) -> None:
        sighting = self._live_zone(track_id, now)
        if sighting is not None:
            # Visible, just outside the gate area: the dwell clock must not run,
            # or a car waiting past the zone edge reads as an exit.
            sighting.last_seen = now

    def _in_zone(self, point: tuple[float, float]) -> bool:
        return self.zone is None or point_in_polygon(Point(*point), self.zone)

    def _step_counts(self, previous: tuple[float, float], point: tuple[float, float]) -> bool:
        """Is this step eligible for crossing at all?

        With a zone configured, a step only counts while it stays inside it: a
        vehicle entering the frame from the far side of the picture, or leaving
        it, must not be treated as crossing the barrier line.
        """
        if self.zone is None:
            return True
        midpoint = ((previous[0] + point[0]) / 2.0, (previous[1] + point[1]) / 2.0)
        return self._in_zone(midpoint)
