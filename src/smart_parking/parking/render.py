"""Overlay rendering for the annotated demo feed.

Only the operator view lives here - the pipeline never mutates its input frame
for logic reasons, so what you see is provably what the AI decided.
"""

from __future__ import annotations

import cv2
import numpy as np

from smart_parking.core.schemas import SlotState, SlotStatus, Track
from smart_parking.parking.gate_logic import ENTRY

STATUS_COLORS = {
    SlotStatus.FREE: (0, 200, 0),
    SlotStatus.PENDING_OCCUPIED: (0, 220, 220),
    SlotStatus.OCCUPIED: (0, 0, 220),
    SlotStatus.PENDING_FREE: (0, 140, 255),
    SlotStatus.UNKNOWN: (140, 140, 140),
}
BOX_COLOR = (255, 60, 60)
FOOTPRINT_COLOR = (0, 255, 255)


def _polygon_pts(poly, w: int, h: int) -> np.ndarray:
    return np.array([[int(p.x * w), int(p.y * h)] for p in poly], dtype=np.int32)


def draw_slots(frame, slots, states: dict[str, SlotState]) -> None:
    h, w = frame.shape[:2]
    for slot_id, poly in slots.items():
        state = states.get(slot_id)
        status = state.status if state else SlotStatus.FREE
        color = STATUS_COLORS[status]
        pts = _polygon_pts(poly, w, h)
        overlay = frame.copy()
        cv2.fillPoly(overlay, [pts], color)
        cv2.addWeighted(overlay, 0.22, frame, 0.78, 0, frame)
        cv2.polylines(frame, [pts], True, color, 2)
        label = f"{slot_id} {status.value.replace('PENDING_', 'P-')}"
        cx, cy = pts[:, 0].mean(), pts[:, 1].mean()
        cv2.putText(frame, label, (int(cx) - 30, int(cy)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 2)


def draw_tracks(frame, tracks: list[Track], occupied_tracks: set[int]) -> None:
    for tr in tracks:
        x1, y1, x2, y2 = (int(v) for v in tr.bbox)
        color = (0, 0, 255) if tr.track_id in occupied_tracks else BOX_COLOR
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        fx1, fy1, fx2, fy2 = (int(v) for v in tr.footprint)
        cv2.rectangle(frame, (fx1, fy1), (fx2, fy2), FOOTPRINT_COLOR, 1)
        cv2.putText(
            frame,
            f"#{tr.track_id} {tr.class_name} {tr.confidence:.2f}",
            (x1, max(0, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
        )


def draw_hud(frame, summary: dict, fps: float, extra: str = "") -> None:
    h, w = frame.shape[:2]
    panel_h = 62
    cv2.rectangle(frame, (0, 0), (w, panel_h), (30, 25, 20), -1)
    line1 = (
        f"TOTAL {summary.get('total', 0)}   "
        f"OCCUPIED {summary.get('occupied', 0)}   "
        f"FREE {summary.get('available', 0)}   "
        f"UNKNOWN {summary.get('unknown', 0)}"
    )
    cv2.putText(frame, line1, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2)
    cv2.putText(
        frame,
        f"{fps:5.1f} FPS   {extra}",
        (12, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (0, 220, 255),
        1,
    )


GATE_ZONE_COLOR = (190, 160, 90)
GATE_LINE_COLOR = {
    ENTRY: (0, 0, 230),
    "EXIT": (0, 200, 0),
}


def _normalised_pts(points, w: int, h: int) -> np.ndarray:
    """Gate geometry is stored 0-1; the pixels it describes depend on the frame."""
    return np.array([[int(x * w), int(y * h)] for x, y in points], dtype=np.int32)


def draw_gate(frame, gate, tracks: list[Track], labels: dict[int, str], hud: str = "") -> None:
    """Gate overlay: the lines, the dwell zone and what the session layer thinks
    each vehicle in view is.

    Nothing here re-decides anything - the geometry is the configured geometry and
    the labels come from the session service - so an operator watching this window
    is watching the same judgement the events record.
    """
    h, w = frame.shape[:2]
    if gate.enabled and gate.zone:
        pts = _normalised_pts(gate.zone, w, h)
        overlay = frame.copy()
        cv2.fillPoly(overlay, [pts], GATE_ZONE_COLOR)
        cv2.addWeighted(overlay, 0.12, frame, 0.78, 0, frame)
        cv2.polylines(frame, [pts], True, GATE_ZONE_COLOR, 1)
        cx, cy = pts[:, 0].mean(), pts[:, 1].mean()
        cv2.putText(
            frame, "exit zone", (int(cx) - 34, int(cy)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.4, GATE_ZONE_COLOR, 1,
        )

    for line in gate.lines:
        pts = _normalised_pts(line.points, w, h)
        a, b = pts[0], pts[1]
        colour = GATE_LINE_COLOR.get(line.role, (140, 140, 140))
        cv2.line(frame, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), colour, 2)
        mid = (int((a[0] + b[0]) / 2), int((a[1] + b[1]) / 2))
        cv2.putText(
            frame,
            f"{line.role} {line.name}",
            (mid[0] + 6, mid[1]),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            colour,
            1,
        )

    for track in tracks:
        x1, y1, x2, y2 = (int(v) for v in track.bbox)
        label = labels.get(track.track_id, "")
        cv2.rectangle(frame, (x1, y1), (x2, y2), BOX_COLOR, 2)
        cv2.putText(
            frame,
            f"#{track.track_id} {label}",
            (x1, max(12, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
        )

    if hud:
        cv2.putText(frame, hud, (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1)
