"""Geometry: normalised slot polygons and footprint overlap scoring.

Polygons are stored in 0-1 coordinates so one calibration survives a camera
resolution change or a resized frame - the doc calls that out as an explicit
Phase 2 acceptance test.
"""

from __future__ import annotations

from smart_parking.core.schemas import Point, Polygon

Box = tuple[float, float, float, float]


def to_pixels(polygon: Polygon, width: int, height: int) -> Polygon:
    return tuple(Point(p.x * width, p.y * height) for p in polygon)


def to_normalised(polygon: Polygon, width: int, height: int) -> Polygon:
    return tuple(Point(p.x / width, p.y / height) for p in polygon)


def polygon_from_points(points) -> Polygon:
    return tuple(Point(float(x), float(y)) for x, y in points)


def slots_from_config(entries, camera_id: str = "cam1") -> dict[str, Polygon]:
    """Turn a slots JSON list into {slot_code: normalised polygon}."""
    out: dict[str, Polygon] = {}
    for entry in entries:
        code = entry.get("code") or entry.get("slot_code")
        if not code:
            raise ValueError(f"slot entry without a code in camera {camera_id}: {entry!r}")
        if code in out:
            raise ValueError(f"duplicate slot code {code!r} in camera {camera_id}")
        out[code] = polygon_from_points(entry["polygon"])
    if not out:
        raise ValueError(f"camera {camera_id} has no slots configured")
    return out


def polygon_area(polygon: Polygon) -> float:
    n = len(polygon)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = polygon[i].x, polygon[i].y
        x2, y2 = polygon[(i + 1) % n].x, polygon[(i + 1) % n].y
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def point_in_polygon(pt: Point, polygon: Polygon) -> bool:
    """Ray casting. Edge behaviour is irrelevant here; centroids rarely land
    exactly on a slot line, and overlap scoring covers the rest."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i].x, polygon[i].y
        xj, yj = polygon[j].x, polygon[j].y
        if (yi > pt.y) != (yj > pt.y) and pt.x < (xj - xi) * (pt.y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _clip_subject(subject, clip):
    """Sutherland-Hodgman: clip a convex polygon by an axis-aligned rect."""
    x_min, y_min, x_max, y_max = clip
    out = list(subject)
    out = _clip_edge(out, lambda p: p[0] >= x_min, lambda p, q: _cross_x(p, q, x_min))
    out = _clip_edge(out, lambda p: p[0] <= x_max, lambda p, q: _cross_x(p, q, x_max))
    out = _clip_edge(out, lambda p: p[1] >= y_min, lambda p, q: _cross_y(p, q, y_min))
    out = _clip_edge(out, lambda p: p[1] <= y_max, lambda p, q: _cross_y(p, q, y_max))
    return out


def _cross_x(p, q, x):
    if q[0] == p[0]:
        return (x, q[1])
    t = (x - p[0]) / (q[0] - p[0])
    return (x, p[1] + t * (q[1] - p[1]))


def _cross_y(p, q, y):
    if q[1] == p[1]:
        return (q[0], y)
    t = (y - p[1]) / (q[1] - p[1])
    return (p[0] + t * (q[0] - p[0]), y)


def _clip_edge(poly, inside, intersect):
    if not poly:
        return []
    out = []
    prev = poly[-1]
    prev_in = inside(prev)
    for cur in poly:
        cur_in = inside(cur)
        if cur_in:
            if not prev_in:
                out.append(intersect(prev, cur))
            out.append(cur)
        elif prev_in:
            out.append(intersect(prev, cur))
        prev, prev_in = cur, cur_in
    return out


def _polygon_area_pts(poly) -> float:
    n = len(poly)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def box_polygon(box: Box) -> list[tuple[float, float]]:
    x1, y1, x2, y2 = box
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def intersection_area(subject: Polygon | list, box: Box) -> float:
    """Area of (convex polygon ∩ rect).

    The vehicle footprint is always a rectangle, so clipping the slot polygon
    against it is exact and needs no third-party dependency.
    """
    pts = [(p.x, p.y) for p in subject] if hasattr(subject[0], "x") else list(subject)
    if len(pts) < 3:
        return 0.0
    return _polygon_area_pts(_clip_subject(pts, box))


def overlap_score(footprint: Box, slot_polygon: Polygon) -> float:
    """How much of a slot a vehicle's ground footprint covers.

    Score = area(footprint ∩ slot) / area(slot), per the architecture doc.
    Using the slot as the denominator keeps the metric comparable across slots
    of different pixel sizes, which matters in an angled view where far slots
    are much smaller than near ones.
    """
    area = polygon_area(slot_polygon)
    if area <= 0:
        return 0.0
    return min(1.0, intersection_area(slot_polygon, footprint) / area)


def box_area(box: Box) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def containment_score(footprint: Box, slot_polygon: Polygon) -> float:
    """How much of a vehicle's footprint sits inside one slot.

    This, not ``overlap_score``, is what we assign vehicles with. The doc's
    slot-denominated metric answers "how full is this bay", which misfires for
    a small car in a big far-row bay: a car parked perfectly inside can score
    0.2 and read as unassigned. Asking the question the other way round gives
    ~1.0 for the same car and stays 0 for a car in the aisle.
    """
    area = box_area(footprint)
    if area <= 0:
        return 0.0
    return min(1.0, intersection_area(slot_polygon, footprint) / area)


def describe_scores(scores: list[tuple[str, float]], top: int = 3) -> str:
    return ", ".join(f"{sid}={score:.2f}" for sid, score in scores[:top])
