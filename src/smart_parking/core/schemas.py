"""Shared domain types for the AI layers.

Nothing in this module may import from db/, api/ or services/ - the AI pipeline
has to stay runnable against plain video files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

# COCO ids for the vehicle classes we care about.
VEHICLE_CLASS_IDS: dict[int, str] = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


class SlotStatus(str, Enum):
    FREE = "FREE"
    PENDING_OCCUPIED = "PENDING_OCCUPIED"
    OCCUPIED = "OCCUPIED"
    PENDING_FREE = "PENDING_FREE"
    UNKNOWN = "UNKNOWN"


class SlotType(str, Enum):
    REGULAR = "REGULAR"
    DISABLED = "DISABLED"
    RESERVED = "RESERVED"
    EV = "EV"
    STAFF = "STAFF"


class SessionStatus(str, Enum):
    """In-memory parking session states (Phase 6).

    NEEDS_REVIEW is a completed session nobody should trust the plate of: the
    doc's own rule is that an uncertain read still books the parking, but it has
    to be visible to an operator rather than silently accepted.
    """

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ANOMALY = "ANOMALY"  # duplicate ACTIVE for a plate, or an exit with no session


class CameraRole(str, Enum):
    LOT = "LOT"
    GATE_ENTRY = "GATE_ENTRY"
    GATE_EXIT = "GATE_EXIT"


class Role(str, Enum):
    """Who a token belongs to, as the doc's section D names them.

    The vocabulary lives next to the other domain enums because the database's
    CHECK constraint, the JWT claim and the API's dependency all have to spell
    the same four words; two lists that drift are a silent permission bug.
    """

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"
    CUSTOMER = "CUSTOMER"


@dataclass(frozen=True)
class Point:
    x: float
    y: float


Polygon = tuple[Point, ...]


@dataclass
class Detection:
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels
    confidence: float
    class_id: int
    class_name: str

    @property
    def centroid(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


@dataclass
class Track(Detection):
    track_id: int
    # Ground footprint: the lower band of the box, which is the part that
    # actually touches the tarmac. See doc section B - the roof of a car in an
    # angled view overlaps neighbouring slots.
    footprint_ratio: float = 0.4

    @property
    def footprint(self) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = self.bbox
        return (x1, y2 - (y2 - y1) * self.footprint_ratio, x2, y2)

    @property
    def footprint_polygon(self) -> Polygon:
        x1, y1, x2, y2 = self.footprint
        return (Point(x1, y1), Point(x2, y1), Point(x2, y2), Point(x1, y2))


@dataclass(frozen=True)
class PlateDetection:
    """One candidate plate region found inside a vehicle box (Phase 5).

    Coordinates are pixels in the *frame*, not the crop, so a caller can draw
    or associate the result without knowing how the crop was taken.
    """

    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2
    confidence: float  # text-region detector score, 0-1
    track_id: int
    timestamp: float

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass(frozen=True)
class PlateObservation:
    """A single OCR attempt on a single plate crop. Never a final answer."""

    track_id: int
    raw_text: str
    plate: str | None  # normalised + validated; None when rejected
    ocr_confidence: float
    detector_confidence: float
    timestamp: float
    bbox: tuple[float, float, float, float]
    repaired: bool = False

    @property
    def accepted(self) -> bool:
        return self.plate is not None


@dataclass(frozen=True)
class PlateResult:
    """The stable plate for a track, after multi-frame voting."""

    track_id: int
    plate_text: str
    plate_confidence: float  # detector score of the winning observation
    ocr_confidence: float  # mean OCR score over the agreeing observations
    timestamp: float
    bbox: tuple[float, float, float, float]
    stable: bool
    observations: int  # agreeing votes in the window
    agreement: float  # agreeing / total votes in the window
    class_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "plate": self.plate_text,
            "plate_confidence": round(self.plate_confidence, 3),
            "ocr_confidence": round(self.ocr_confidence, 3),
            "ts": self.timestamp,
            "bbox": [round(v, 1) for v in self.bbox],
            "stable": self.stable,
            "observations": self.observations,
            "agreement": round(self.agreement, 3),
            "class": self.class_name,
        }


@dataclass(frozen=True)
class GateCrossing:
    """One vehicle crossing one virtual gate line (Phase 6).

    Geometry only: it says nothing about sessions or plates, so the crossing
    rules can be tested against centroid sequences with no OCR in the loop.
    """

    camera_id: str
    track_id: int
    line: str  # configured line name
    role: str  # "ENTRY" or "EXIT", from the line's configuration
    timestamp: float
    direction: str  # "positive" | "negative" | "dwell" (last seen in the zone)
    centroid: tuple[float, float]

    @property
    def idempotency_key(self) -> tuple[str, int, str]:
        """The doc's key for "each track fires each event at most once"."""
        return (self.camera_id, self.track_id, self.role)


@dataclass
class Frame:
    camera_id: str
    index: int
    timestamp: float
    image: np.ndarray


@dataclass
class FrameView:
    """One annotated frame plus what the pipeline concluded about it (Phase 8).

    The doc's section E wants two live views of the same camera: an MJPEG stream
    and a WebSocket overlay of boxes/labels. Passing both out of one object means
    the picture and the JSON cannot describe different frames, which is what
    happens when a viewer gets an image from the encoder and geometry from a
    later inference tick.

    `labels` maps track id to the text the pipeline drew on the box, so a browser
    reads exactly what an operator's `--show` window reads. `summary` is the
    camera's own counters (occupancy for a lot, open sessions for a gate).
    """

    camera_id: str
    seq: int
    timestamp: float
    image: np.ndarray
    tracks: list[Track] = field(default_factory=list)
    labels: dict[int, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    def overlay(self) -> dict[str, Any]:
        """The JSON half of the overlay contract - no numpy, no image."""
        return {
            "camera_id": self.camera_id,
            "seq": self.seq,
            "ts": self.timestamp,
            "summary": dict(self.summary),
            "tracks": [
                {
                    "track_id": t.track_id,
                    "bbox": [round(float(v), 1) for v in t.bbox],
                    "confidence": round(float(t.confidence), 3),
                    "label": self.labels.get(t.track_id),
                }
                for t in self.tracks
            ],
        }


@dataclass
class SlotState:
    slot_id: str
    status: SlotStatus
    since: float
    score: float = 0.0
    track_id: int | None = None


@dataclass
class Event:
    type: str
    camera_id: str
    timestamp: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "camera_id": self.camera_id,
            "ts": self.timestamp,
            "payload": self.payload,
        }


# Event type constants - the contract between the AI layers and services/.
SLOT_OCCUPIED = "slot_occupied"
SLOT_FREED = "slot_freed"
# The occupancy engine's first committed reading of every bay it was built for.
# A bay the camera can see but that has never held a car produces no transition,
# so without this it would keep the UNKNOWN its row was created with - and
# UNKNOWN is the name for "the camera is not telling us", not for "empty".
SLOT_SNAPSHOT = "slot_snapshot"
SUMMARY = "summary"
VEHICLE_ENTERED = "vehicle_entered"
VEHICLE_EXITED = "vehicle_exited"
PLATE_READ = "plate_read"
VIOLATION = "violation"
CAMERA_STATUS = "camera_status"

# Anomaly reasons - the codes the session state machine raises and the persisted
# session rows store. They live here because both sides of the seam have to say
# the same thing: a reason invented independently in each layer is two vocabularies
# for one fact.
DUPLICATE_PLATE = "duplicate_active_session"
NO_ENTRY = "exit_without_entry"
# A dwell exit is stamped at the vehicle's last sighting in the zone, which can be
# earlier than the entry of the session the same track id opened. Two timestamps
# that cannot describe one visit are an anomaly, not a negative duration.
UNORDERED_EXIT = "exit_before_entry"
