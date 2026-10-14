"""Camera calibration: draw slot polygons on a snapshot, save normalised JSON.

Run ``python scripts/calibrate.py --source <clip>`` and click the four corners
of each bay. Coordinates are stored in 0-1 space so the same file works at any
camera resolution.

``--gate`` draws the Phase 6 equivalent for an entrance camera: two-point entry
and exit lines and the dwell zone, written to a gate file the loader accepts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from smart_parking.core.logging import get_logger
from smart_parking.parking.gate_logic import ENTRY, EXIT

log = get_logger("parking.calibration")

HELP = """
click corners of a slot   -> 4th click closes it
n / enter                 -> start next slot
u                         -> undo last point
x                         -> delete last slot
c                         -> clear all slots
s                         -> save JSON
q / esc                   -> quit (s first if you want the file)
"""


def grab_snapshot(source: str) -> np.ndarray:
    """First usable frame from a file or camera - the calibration canvas."""
    from smart_parking.video.sources import open_capture

    cap = open_capture(source)
    try:
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError(f"could not read a frame from {source}")
        return frame
    finally:
        cap.release()


def draw_grid(frame: np.ndarray, step_px: int = 50) -> np.ndarray:
    """Overlay a coordinate grid.

    Not part of the operator UI: it exists so a slot file can be authored or
    corrected from a screenshot without the interactive mouse tool.
    """
    out = frame.copy()
    h, w = out.shape[:2]
    for x in range(0, w, step_px):
        major = (x // step_px) % 2 == 0
        cv2.line(out, (x, 0), (x, h), (0, 190, 255) if major else (70, 70, 70), 1)
        if major:
            cv2.putText(out, str(x), (x + 2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 200, 255), 1)
    for y in range(0, h, step_px):
        major = (y // step_px) % 2 == 0
        cv2.line(out, (0, y), (w, y), (0, 190, 255) if major else (70, 70, 70), 1)
        if major:
            cv2.putText(out, str(y), (2, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 200, 255), 1)
    return out


class SlotCollector:
    """Mouse-driven polygon capture, shared by the CLI tool and the tests."""

    def __init__(self, frame: np.ndarray, prefix: str = "A") -> None:
        self.frame = frame
        self.h, self.w = frame.shape[:2]
        self.prefix = prefix
        self.slots: dict[str, list[tuple[float, float]]] = {}
        self.current: list[tuple[float, float]] = []

    # -- naming ------------------------------------------------------------
    def next_code(self) -> str:
        i = len(self.slots)
        while True:
            code = f"{self.prefix}{i + 1}"
            if code not in self.slots:
                return code
            i += 1

    def add_point(self, x: int, y: int) -> bool:
        """Returns True when the 4th point completed a slot."""
        self.current.append((x / self.w, y / self.h))
        if len(self.current) >= 4:
            self.slots[self.next_code()] = self.current
            self.current = []
            return True
        return False

    def undo(self) -> None:
        if self.current:
            self.current.pop()

    def delete_last(self) -> None:
        if self.slots:
            self.slots.pop(next(reversed(self.slots)))

    def to_config(self, camera_id: str = "cam1") -> dict:
        return {
            "camera_id": camera_id,
            "frame_size": {"width": self.w, "height": self.h},
            "coordinate_space": "normalised_0_1",
            "slots": [
                {
                    "code": code,
                    "polygon": [[round(x, 5), round(y, 5)] for x, y in poly],
                    "type": "REGULAR",
                }
                for code, poly in self.slots.items()
            ],
        }

    def save(self, path: str | Path, camera_id: str = "cam1") -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_config(camera_id), indent=2), encoding="utf-8")
        log.info("saved %d slots -> %s", len(self.slots), p)
        return p

    # -- rendering ---------------------------------------------------------
    def render(self) -> np.ndarray:
        out = self.frame.copy()
        for code, poly in self.slots.items():
            pts = np.array([[int(x * self.w), int(y * self.h)] for x, y in poly], np.int32)
            cv2.polylines(out, [pts], True, (0, 220, 0), 2)
            cv2.putText(out, code, tuple(pts[0] + np.array([4, -6])), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 2)
        for x, y in self.current:
            px, py = int(x * self.w), int(y * self.h)
            cv2.circle(out, (px, py), 4, (0, 0, 255), -1)
        return out


def interactive(source: str, out_path: str | Path, camera_id: str = "cam1") -> int:
    frame = grab_snapshot(source)
    collector = SlotCollector(frame)
    win = "Calibration"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            collector.add_point(x, y)

    cv2.setMouseCallback(win, on_mouse)
    log.info(HELP)

    while True:
        canvas = collector.render()
        cv2.putText(canvas, f"slots={len(collector.slots)} pending={len(collector.current)}  (s)save (q)quit",
                    (10, canvas.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.imshow(win, canvas)
        key = cv2.waitKey(30) & 0xFF
        if key in (27, ord("q")):
            break
        if key in (ord("s"),):
            collector.save(out_path, camera_id)
            break
        if key in (ord("n"), 13):
            if collector.current:
                collector.slots[collector.next_code()] = collector.current
                collector.current = []
        if key == ord("u"):
            collector.undo()
        if key == ord("x"):
            collector.delete_last()
        if key == ord("c"):
            collector.slots.clear()
            collector.current = []

    cv2.destroyAllWindows()
    return len(collector.slots)


GATE_HELP = """
i           start an ENTRY line     o   start an EXIT line   (then click both ends)
z           start the dwell zone, n or enter to close it (3 clicks minimum)
d           flip the last line's direction
u           undo the last click    x   delete the last line / the zone
c           clear everything       s   save JSON            q   quit

Direction: a line is drawn from its first click to its second. "positive" means a
vehicle crossing from the left of that arrow to its right; "negative" is the other
way round. One lane in each direction is the same segment drawn twice, once per
role, and `d` is how you make the second one face the other way.
"""


@dataclass
class GateLineDraft:
    name: str
    role: str
    points: list[tuple[float, float]]
    direction: str = "positive"


class GateCollector:
    """Mouse-driven entry/exit line and zone capture, shared by the CLI and tests.

    The output is validated by `GateConfig` on the way in, so a file this writes
    cannot be one the pipeline then refuses to load.
    """

    def __init__(self, frame: np.ndarray) -> None:
        self.frame = frame
        self.h, self.w = frame.shape[:2]
        self.lines: list[GateLineDraft] = []
        self.zone: list[tuple[float, float]] = []
        # What the next click belongs to: a line of this role, or the zone.
        self.role: str | None = None
        self.drawing_zone = False
        self.current: list[tuple[float, float]] = []
        self._counts = {ENTRY: 0, EXIT: 0}

    # -- capture -------------------------------------------------------------
    def start_line(self, role: str) -> None:
        self.drawing_zone = False
        self.role = role
        self.current = []

    def start_zone(self) -> None:
        self.role = None
        self.current = []
        self.drawing_zone = True

    def close_zone(self) -> bool:
        """True when the polygon is usable; fewer than 3 points is not a zone."""
        self.drawing_zone = False
        return len(self.zone) >= 3

    def add_point(self, x: int, y: int) -> str | None:
        """Record one click, returning what it completed ("line", "point", None)."""
        point = (x / self.w, y / self.h)
        if self.drawing_zone:
            self.zone.append(point)
            return "point"
        if self.role is None:
            return None
        self.current.append(point)
        if len(self.current) < 2:
            return None
        self._counts[self.role] += 1
        stem = "in" if self.role == ENTRY else "out"
        self.lines.append(
            GateLineDraft(
                name=f"{stem}{self._counts[self.role]}",
                role=self.role,
                points=self.current,
            )
        )
        self.current = []
        self.role = None
        return "line"

    def flip_direction(self) -> bool:
        if not self.lines:
            return False
        line = self.lines[-1]
        line.direction = "negative" if line.direction == "positive" else "positive"
        return True

    def undo(self) -> None:
        if self.current:
            self.current.pop()
        elif self.drawing_zone and self.zone:
            self.zone.pop()

    def delete_last(self) -> None:
        if self.lines:
            self.lines.pop()
        else:
            self.zone = []
            self.drawing_zone = False

    def clear(self) -> None:
        self.lines = []
        self.zone = []
        self.current = []
        self.role = None
        self.drawing_zone = False
        self._counts = {ENTRY: 0, EXIT: 0}

    # -- output --------------------------------------------------------------
    def to_config(self, camera_id: str = "gate_cam1", notes: str = "") -> dict:
        def scaled(points):
            return [[round(x, 5), round(y, 5)] for x, y in points]

        return {
            "camera_id": camera_id,
            "frame_size": {"width": self.w, "height": self.h},
            "coordinate_space": "normalised_0_1",
            "notes": notes
            or "Drawn with scripts/calibrate.py --gate. Check each line's direction "
            "against the way traffic actually flows before trusting it on a live gate.",
            "lines": [
                {
                    "name": line.name,
                    "role": line.role,
                    "direction": line.direction,
                    "points": scaled(line.points),
                }
                for line in self.lines
            ],
            "zone": scaled(self.zone),
        }

    def save(self, path: str | Path, camera_id: str = "gate_cam1") -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_config(camera_id), indent=2), encoding="utf-8")
        log.info("saved %d gate lines, zone of %d points -> %s", len(self.lines), len(self.zone), p)
        return p

    # -- rendering -----------------------------------------------------------
    def render(self) -> np.ndarray:
        out = self.frame.copy()
        if self.zone:
            pts = np.array([[int(x * self.w), int(y * self.h)] for x, y in self.zone], np.int32)
            cv2.polylines(out, [pts], not self.drawing_zone, (190, 160, 90), 2)
        for line in self.lines:
            (ax, ay), (bx, by) = [
                (int(x * self.w), int(y * self.h)) for x, y in line.points
            ]
            colour = (0, 0, 230) if line.role == ENTRY else (0, 200, 0)
            cv2.arrowedLine(out, (ax, ay), (bx, by), colour, 2, tipLength=0.08)
            cv2.putText(
                out, f"{line.name} {line.role[0]}/{line.direction[0]}",
                (min(ax, bx) + 6, min(ay, by) - 6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1,
            )
        for x, y in self.current:
            cv2.circle(out, (int(x * self.w), int(y * self.h)), 5, (0, 0, 255), -1)
        return out


def interactive_gate(source: str, out_path: str | Path, camera_id: str = "gate_cam1") -> int:
    """Open the gate drawing tool; returns the number of lines saved."""
    frame = grab_snapshot(source)
    collector = GateCollector(frame)
    win = "Gate calibration"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            collector.add_point(x, y)

    cv2.setMouseCallback(win, on_mouse)
    log.info(GATE_HELP)

    while True:
        canvas = collector.render()
        pending = "zone" if collector.drawing_zone else (collector.role or "-")
        cv2.putText(
            canvas,
            f"lines={len(collector.lines)} zone={len(collector.zone)} drawing={pending}"
            "  (s)save (q)quit",
            (10, canvas.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2,
        )
        cv2.imshow(win, canvas)
        key = cv2.waitKey(30) & 0xFF
        if key in (27, ord("q")):
            break
        elif key == ord("i"):
            collector.start_line(ENTRY)
        elif key == ord("o"):
            collector.start_line(EXIT)
        elif key == ord("z"):
            collector.start_zone()
        elif key in (ord("n"), 13):
            if collector.drawing_zone and not collector.close_zone():
                log.warning("a zone needs at least 3 clicks - %d given", len(collector.zone))
        elif key == ord("d"):
            collector.flip_direction()
        elif key == ord("u"):
            collector.undo()
        elif key == ord("x"):
            collector.delete_last()
        elif key == ord("c"):
            collector.clear()
        elif key == ord("s"):
            if not collector.lines:
                log.error("no gate lines drawn - nothing to save")
                break
            collector.save(out_path, camera_id)
            break

    cv2.destroyAllWindows()
    return len(collector.lines)
