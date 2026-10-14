"""Response and request models for the HTTP surface (Phase 8).

Two rules run through this module:

* **`password_hash` is never a field on anything.** The ORM row has it; the
  response model does not, so serialising a user cannot leak it by accident.
* **A plate is two fields, not one.** `plate` is present only for a role allowed
  to see it and `plate_masked` is always present, so a dashboard never has to
  guess why a field is missing and a client can render one template for both.
  Masking goes through `security.mask_plate` - the same call the WebSocket hub
  makes - because three implementations of a redaction rule is three chances to
  leak.

Datetimes are naive UTC in the rows (see `db/base.py`), so they are stamped with
an offset on the way out rather than left ambiguous.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from smart_parking.api.security import mask_plate, valid_email
from smart_parking.core.schemas import Role
from smart_parking.db.models import AuditLog, Camera, LicensePlate, ParkingSession, ParkingSlot, User

PLATE_FIELD = Field(description="Full plate - present only for ADMIN/OPERATOR")


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def naive_utc(value: datetime | None) -> datetime | None:
    """The inverse of `iso`, for the timestamps a client sends back.

    The rows hold naive UTC (`db/base.py`), so a query parameter or a body field
    has to land in the same frame of reference or every comparison in the
    database is between two clocks.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# -- credentials -------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        # valid_email raises ValueError, which pydantic turns into a 422 - the
        # same shape as any other malformed body.
        return valid_email(v)


class RefreshRequest(BaseModel):
    refresh_token: str


class UserCreateRequest(BaseModel):
    email: str
    password: str
    role: Role = Role.VIEWER

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return valid_email(v)


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    # When the access token dies, so a client can refresh before its next call
    # rather than discovering the expiry with a 401.
    access_expires_at: str
    role: Role
    email: str


class LogoutResponse(BaseModel):
    """`POST /auth/logout`: what was withdrawn, and from when.

    A logout has to answer with the cutoff it applied, because "signed out" in a
    server that keeps no session is a statement about timestamps rather than about
    a live connection. A client that is told `invalidated_from` can prove its own
    tokens are dead without decoding them.
    """

    revoked_at: str
    email: str


class WsTicketResponse(BaseModel):
    """One-time credentials for the socket endpoints.

    The ticket is not a token: it names an account to a handshake for
    `api.ws_ticket_ttl_s` seconds, is spent by the first connect that presents it,
    and can call nothing else. It opens either socket, because what it grants is
    the same identity the bearer token was carrying, and both endpoints are
    read-only and re-check the account row on redemption.
    """

    ticket: str
    expires_at: str


class UserResponse(BaseModel):
    """Deliberately has no password field of any kind."""

    id: int
    email: str
    role: Role
    is_active: bool
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: User) -> "UserResponse":
        return cls(
            id=row.id,
            email=row.email,
            role=row.role,
            is_active=row.is_active,
            created_at=iso(row.created_at),
        )


class MeResponse(UserResponse):
    reveal_plates: bool = False


# -- paging ------------------------------------------------------------------


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class SessionPage(Page):
    items: list["SessionResponse"]


class PlatePage(Page):
    items: list["PlateResponse"]


class AuditPage(Page):
    items: list["AuditResponse"] = Field(default_factory=list)


# -- cameras and slots -------------------------------------------------------


class CameraResponse(BaseModel):
    camera_id: str
    name: str
    role: str
    source: str | None
    slots_file: str | None
    gate_file: str | None
    fps_limit: float
    enabled: bool
    last_seen_at: str | None = None

    @classmethod
    def from_row(cls, row: Camera) -> "CameraResponse":
        return cls(
            camera_id=row.camera_id,
            name=row.name,
            role=row.role.value,
            source=row.source,
            slots_file=row.slots_file,
            gate_file=row.gate_file,
            fps_limit=row.fps_limit,
            enabled=row.enabled,
            last_seen_at=iso(row.last_seen_at),
        )


class CameraLiveResponse(BaseModel):
    """What the streaming layer knows about this camera right now.

    Separate from the row because the row is configuration and this is a running
    process: `frames_processed` belongs to whichever pipeline pass is live, and
    `synthetic_replay` is the flag that says a picture here is a rendered fixture
    rather than a field camera.
    """

    camera_id: str
    streaming: bool
    frames_available: bool
    role: str
    synthetic_replay: bool = False
    read_plates: bool = False
    loop: bool = False
    passes: int = 0
    frames_processed: int | None = None
    error: str | None = None


class CameraRegisterRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)
    name: str = ""
    role: Literal["LOT", "GATE_ENTRY", "GATE_EXIT"] = "LOT"
    source: str | None = None
    slots_file: str | None = None
    gate_file: str | None = None
    fps_limit: float = 15.0


class CameraUpdateRequest(BaseModel):
    name: str | None = None
    source: str | None = None
    slots_file: str | None = None
    gate_file: str | None = None
    fps_limit: float | None = None
    enabled: bool | None = None


class SlotResponse(BaseModel):
    id: int
    camera_id: str
    slot_code: str
    slot_type: str
    current_status: str
    status_since: str | None = None
    occupied_by_track_id: int | None = None
    polygon: list[Any] = Field(default_factory=list)
    # The doc's `session_id?`: the live visit holding this bay, when one exists.
    session_id: str | None = None

    @classmethod
    def from_row(cls, row: ParkingSlot, *, session_id: str | None = None) -> "SlotResponse":
        return cls(
            id=row.id,
            camera_id=row.camera_id,
            slot_code=row.slot_code,
            slot_type=row.slot_type.value,
            current_status=row.current_status.value,
            status_since=iso(row.status_since),
            occupied_by_track_id=row.occupied_by_track_id,
            polygon=list(row.polygon or []),
            session_id=session_id,
        )


class SlotCreateRequest(BaseModel):
    camera_id: str
    slot_code: str = Field(min_length=1, max_length=32)
    slot_type: Literal["REGULAR", "DISABLED", "RESERVED", "EV", "STAFF"] = "REGULAR"
    # [[x, y], ...] in the frame's own pixels, the same shape the calibration file
    # writes - a browser drawing a polygon sends pixel points, not fractions.
    polygon: list[list[float]] = Field(min_length=3)
    frame_size: dict[str, int] | None = None

    @field_validator("polygon")
    @classmethod
    def _points(cls, v: list[list[float]]) -> list[list[float]]:
        if any(len(point) != 2 for point in v):
            raise ValueError("a polygon point is [x, y]")
        return [[float(point[0]), float(point[1])] for point in v]


class PolygonUpdateRequest(BaseModel):
    polygon: list[list[float]] = Field(min_length=3)
    frame_size: dict[str, int] | None = None

    @field_validator("polygon")
    @classmethod
    def _points(cls, v: list[list[float]]) -> list[list[float]]:
        if any(len(point) != 2 for point in v):
            raise ValueError("a polygon point is [x, y]")
        return [[float(point[0]), float(point[1])] for point in v]


class LotSummaryResponse(BaseModel):
    camera_id: str
    total: int
    occupied: int
    available: int
    # The doc's `reserved` is a *type*, not a state, so it counts bays marked
    # RESERVED rather than pretending to be a fourth status the FSM has never
    # defined.
    reserved: int
    # FREE and OCCUPIED are measured; UNKNOWN means the camera is not telling us,
    # and reporting that as empty would be a lie with a nicer number.
    unknown: int
    # FREE over the bays that have a committed state at all: dividing by a total
    # that includes UNKNOWN would report a camera outage as spare capacity.
    occupancy_rate: float
    from_live_frames: bool = False


# -- sessions and plates -----------------------------------------------------


class SessionResponse(BaseModel):
    id: int
    source_session_id: str
    camera_id: str
    entry_track_id: int
    exit_track_id: int | None
    plate: str | None = PLATE_FIELD
    plate_masked: str | None = None
    vehicle_type: str | None = None
    entry_time: str | None = None
    exit_time: str | None = None
    duration_seconds: float | None = None
    status: str
    exit_reason: str | None = None
    plate_confidence: float | None = None
    ocr_confidence: float | None = None
    review_reason: str | None = None
    anomaly_reason: str | None = None

    @classmethod
    def from_row(cls, row: ParkingSession, *, reveal_plates: bool) -> "SessionResponse":
        plate_text = row.plate.plate_text if row.plate is not None else None
        return cls(
            id=row.id,
            source_session_id=row.source_session_id,
            camera_id=row.camera_id,
            entry_track_id=row.entry_track_id,
            exit_track_id=row.exit_track_id,
            plate=plate_text if reveal_plates else None,
            plate_masked=mask_plate(plate_text),
            vehicle_type=row.vehicle.vehicle_type if row.vehicle is not None else None,
            entry_time=iso(row.entry_time),
            exit_time=iso(row.exit_time),
            duration_seconds=row.duration_seconds,
            status=row.status.value,
            exit_reason=row.exit_reason,
            plate_confidence=row.plate_confidence,
            ocr_confidence=row.ocr_confidence,
            review_reason=row.review_reason,
            anomaly_reason=row.anomaly_reason,
        )


class PlateCorrectionRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=16)


class SessionCloseRequest(BaseModel):
    """An operator closing a visit the gate never saw leave.

    `exit_time` is optional because the honest default is *now*, not a time the
    operator estimates: an estimate looks identical to a measurement in the
    duration column.
    """

    exit_time: datetime | None = None
    reason: str = Field(default="manual", max_length=64)


class PlateResponse(BaseModel):
    id: int
    plate: str | None = PLATE_FIELD
    plate_masked: str | None = None
    confidence: float | None = None
    reads: int
    first_seen_at: str | None = None
    last_seen_at: str | None = None

    @classmethod
    def from_row(cls, row: LicensePlate, *, reveal_plates: bool) -> "PlateResponse":
        return cls(
            id=row.id,
            plate=row.plate_text if reveal_plates else None,
            plate_masked=mask_plate(row.plate_text),
            confidence=row.confidence,
            reads=row.reads,
            first_seen_at=iso(row.first_seen_at),
            last_seen_at=iso(row.last_seen_at),
        )


# -- statistics --------------------------------------------------------------


class OccupancySeriesResponse(BaseModel):
    camera_id: str | None
    bucket: str
    points: list[dict[str, Any]] = Field(default_factory=list)
    # Stating the definition in the payload, because "occupancy over time" has
    # several reasonable answers and a chart cannot show which one it got.
    definition: str = "bays whose occupancy interval covered the sample instant"
    intervals_considered: int = 0


class DurationStatsResponse(BaseModel):
    average_s: float | None
    median_s: float | None
    longest_s: float | None
    sample_size: int
    arrivals_considered: int
    peak_hours: list[dict[str, int]] = Field(default_factory=list)


# -- audit -------------------------------------------------------------------


class AuditResponse(BaseModel):
    id: int
    actor_email: str | None
    action: str
    target_type: str | None
    target_id: str | None
    detail: Any = None
    source_ip: str | None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: AuditLog) -> "AuditResponse":
        return cls(
            id=row.id,
            actor_email=row.actor_email,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            detail=row.detail,
            source_ip=row.source_ip,
            created_at=iso(row.created_at),
        )


# -- health and live ---------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: str
    anpr_enabled: bool
    gpu: dict[str, Any] = Field(default_factory=dict)
    cameras: list[CameraLiveResponse] = Field(default_factory=list)
    ws_clients: int = 0
    frames_in: int = 0
    events_out: int = 0
    dropped_events: int = 0
    # Socket tickets refused as unknown/used/expired. A dashboard that will not
    # connect and an operator who keeps getting logged out look the same from the
    # browser; this number says which side of the handshake is wrong.
    ws_ticket_refusals: int = 0
    uptime_s: float = 0.0


class OverlayFrameResponse(BaseModel):
    """The JSON half of `FrameView`, for a browser drawing its own boxes."""

    camera_id: str
    seq: int
    ts: float
    summary: dict[str, Any] = Field(default_factory=dict)
    tracks: list[dict[str, Any]] = Field(default_factory=list)


Page.model_rebuild()
SessionPage.model_rebuild()
PlatePage.model_rebuild()
AuditPage.model_rebuild()
