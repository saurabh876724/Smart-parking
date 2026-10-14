"""ORM models for the persistence layer (Phase 7).

These are **database rows**, not the domain types in `core/schemas.py`, and the
two are deliberately kept apart: `Track`, `GateCrossing`, `PlateResult` and
`ParkingSession` describe what the camera saw on one frame or one visit, while
the models here describe what the system decided to remember. The conversion
happens once, in `services/persistence.py`, so the AI layers never import
SQLAlchemy and the schema can change without touching a detector.

Naming follows the architecture doc's section D table summary where the project
already has data for it, and stops there: `users`, `reservations`, `tariffs`,
`payments` and `audit_logs` belong to the authentication and billing phases, and
`parking_lots` would be a column of ones with exactly one lot on this machine.

Three deliberate deviations from the doc, each for a reason that is not just
convenience:

* plates are stored as normalised text, not AES-GCM ciphertext with an HMAC
  lookup hash - there is no secret store yet, and an encrypted column with a
  hard-coded key is a false claim of protection. The hash column is where the
  doc's lookup goes once key management exists.
* geometry and payloads are `JSON`, not Postgres `JSONB`. On SQLite that is a
  TEXT round trip; the doc's JSONB is a Postgres-specific accelerator, and the
  two are the same query surface through SQLAlchemy.
* camera ids are the natural key (`gate_cam1`), not a surrogate integer, so a
  `camera_id` column means the same thing in every table and in every log line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smart_parking.core.schemas import CameraRole, Role, SessionStatus, SlotStatus, SlotType
from smart_parking.db.base import Base, TimestampMixin, utcnow

# The COCO classes the detector can report, as a check constraint rather than a
# Postgres enum: a vehicle type is data, and it should not need a migration.
VEHICLE_TYPES = ("car", "motorcycle", "bus", "truck")
EXIT_REASONS = ("line", "dwell")


class Camera(TimestampMixin, Base):
    """One configured viewpoint, keyed by the same id cameras.yaml uses.

    `source` is a reference, not a secret: the doc's `rtsp_url_encrypted` is for
    credentials, and every source in this project is a local file or a camera
    index. `slots_file` / `gate_file` point at the calibration the camera was
    drawn against, which is what makes a row like this reviewable later.
    """

    __tablename__ = "cameras"

    camera_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[CameraRole] = mapped_column(
        Enum(CameraRole, native_enum=False, name="camera_role", create_constraint=True), default=CameraRole.LOT
    )
    source: Mapped[str | None] = mapped_column(String(512))
    slots_file: Mapped[str | None] = mapped_column(String(256))
    gate_file: Mapped[str | None] = mapped_column(String(256))
    fps_limit: Mapped[float] = mapped_column(Float, default=15.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    slots: Mapped[list["ParkingSlot"]] = relationship(
        back_populates="camera", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Camera {self.camera_id} role={self.role.value}>"


class ParkingSlot(Base, TimestampMixin):
    """A bay, its normalised polygon and the status the engine last committed.

    `slot_code` is the identifier the occupancy engine already uses (A1, B2, ...
    from the calibration file), and it is only unique **per camera**, which is
    why the unique key is the pair rather than the code. `current_status` is the
    engine's committed state, not a live frame: the FSM's PENDING_* states never
    reach here, and UNKNOWN is reserved for a camera that stopped reporting.
    """

    __tablename__ = "parking_slots"
    __table_args__ = (UniqueConstraint("camera_id", "slot_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), ForeignKey("cameras.camera_id"))
    slot_code: Mapped[str] = mapped_column(String(32))
    slot_type: Mapped[SlotType] = mapped_column(
        Enum(SlotType, native_enum=False, name="slot_type", create_constraint=True),
        default=SlotType.REGULAR,
    )
    polygon: Mapped[list[Any]] = mapped_column(JSON, default=list)
    frame_size: Mapped[dict[str, int] | None] = mapped_column(JSON)
    # The vocabulary is a database constraint (the enum's CHECK), so a hand
    # written row cannot invent a state the FSM has never defined.
    current_status: Mapped[SlotStatus] = mapped_column(
        Enum(SlotStatus, native_enum=False, name="slot_status", create_constraint=True),
        default=SlotStatus.UNKNOWN,
    )
    status_since: Mapped[datetime | None] = mapped_column(DateTime)
    # The vehicle the engine last saw in this bay, for review only. Never an
    # identity: it is the tracker's number for one continuous sighting.
    occupied_by_track_id: Mapped[int | None] = mapped_column(Integer)

    camera: Mapped[Camera] = relationship(back_populates="slots")

    def __repr__(self) -> str:
        return f"<Slot {self.camera_id}/{self.slot_code} {self.current_status.value}>"


class LicensePlate(Base, TimestampMixin):
    """A plate the OCR stage agreed on, and when it was first and last seen.

    `plate_text` is already normalised by the Phase 5 validator
    (`anpr/validation.py`), which is the only normaliser in the project - a
    second one here would be free to disagree with the voting stage. Unique
    because "which car is this" is exactly that question.
    """

    __tablename__ = "license_plates"

    id: Mapped[int] = mapped_column(primary_key=True)
    plate_text: Mapped[str] = mapped_column(String(16), unique=True)
    country: Mapped[str] = mapped_column(String(8), default="IN")
    # Highest OCR confidence ever seen for this plate, not the last one: a
    # plate read well once should not be downgraded by a worse read later.
    confidence: Mapped[float | None] = mapped_column(Float)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    reads: Mapped[int] = mapped_column(Integer, default=0)

    vehicles: Mapped[list["Vehicle"]] = relationship(back_populates="plate")

    def __repr__(self) -> str:
        return f"<Plate {self.plate_text}>"


class Vehicle(Base, TimestampMixin):
    """Track-independent vehicle identity, where one is actually available.

    A ByteTrack id is explicitly **not** an identity: it is reassigned when a
    car leaves the frame, and two cars can hold the same number an hour apart.
    What survives a sighting is the plate, so a vehicle row is the plate plus
    whatever else was observed about it - here its class, from the detection
    that carried the plate. The doc's registered-owner fields (`owner_id`,
    `is_registered`) arrive with the accounts in the authentication phase.
    """

    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("plate_id", "vehicle_type"),
        CheckConstraint(
            f"vehicle_type IN {VEHICLE_TYPES!r}",
            name="vehicle_type_is_a_detected_class",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plate_id: Mapped[int] = mapped_column(ForeignKey("license_plates.id"))
    vehicle_type: Mapped[str] = mapped_column(String(16))
    is_registered: Mapped[bool] = mapped_column(Boolean, default=False)

    plate: Mapped[LicensePlate] = relationship(back_populates="vehicles")

    def __repr__(self) -> str:
        return f"<Vehicle {self.id} {self.vehicle_type} plate={self.plate_id}>"


class ParkingSession(Base, TimestampMixin):
    """One visit: the crossing in, the crossing out, and what it joined to.

    Mirrors the Phase 6 in-memory `ParkingSession` rather than re-deriving
    anything - `source_session_id` is that object's id, so a row here can be
    tied back to the `ENTER`/`EXIT` line in the console and to the fixture's
    ground truth.

    The nullable columns are the point, not an afterthought: ANPR is not field
    validated, so `plate_id IS NULL` is the normal state of a session from
    footage whose plate was too small to read, and `slot_id IS NULL` because the
    gate camera does not see the bays. A session is a fact about the gate; a
    missing plate makes it a session to review, never a session to discard.

    `entry_time IS NULL` with an exit set is the persisted form of the doc's
    exit-without-entry anomaly. It stays NULL on purpose: an entry time
    invented here would be indistinguishable from one measured by the camera.
    """

    __tablename__ = "parking_sessions"
    __table_args__ = (
        # The doc's "one ACTIVE session per plate / per slot". Postgres-only
        # partial indexes were assumed necessary; SQLite has had them since
        # 3.8, so the constraint is enforced - and tested - here rather than
        # deferred to a database nobody runs yet.
        Index(
            "uq_parking_sessions_active_track",
            "camera_id",
            "entry_track_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "uq_parking_sessions_active_plate",
            "plate_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE' AND plate_id IS NOT NULL"),
            postgresql_where=text("status = 'ACTIVE' AND plate_id IS NOT NULL"),
        ),
        Index(
            "uq_parking_sessions_active_slot",
            "slot_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE' AND slot_id IS NOT NULL"),
            postgresql_where=text("status = 'ACTIVE' AND slot_id IS NOT NULL"),
        ),
        CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0", name="duration_is_not_negative"),
        CheckConstraint(
            "entry_time IS NULL OR exit_time IS NULL OR exit_time >= entry_time",
            name="exit_is_not_before_entry",
        ),
        # Anomalies keep whatever half of the visit was actually measured, and
        # nothing else.
        CheckConstraint(
            "status <> 'COMPLETED' OR (entry_time IS NOT NULL AND exit_time IS NOT NULL)",
            name="completed_needs_both_times",
        ),
        # The two ways the gate logic knows a vehicle left. An unexpected value
        # here means the domain grew and this table was not told.
        CheckConstraint(f"exit_reason IS NULL OR exit_reason IN {EXIT_REASONS!r}", name="exit_reason_is_known"),
        Index("ix_parking_sessions_entry_time", "entry_time"),
        Index("ix_parking_sessions_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The id the in-memory session state machine minted, kept so a duplicate
    # write of the same visit is an update of this row rather than a second row.
    source_session_id: Mapped[str] = mapped_column(String(64), unique=True)
    camera_id: Mapped[str] = mapped_column(String(64), ForeignKey("cameras.camera_id"))
    entry_track_id: Mapped[int] = mapped_column(Integer)
    exit_track_id: Mapped[int | None] = mapped_column(Integer)
    plate_id: Mapped[int | None] = mapped_column(ForeignKey("license_plates.id"))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"))
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("parking_slots.id"))
    entry_camera_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("cameras.camera_id"))
    exit_camera_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("cameras.camera_id"))
    entry_time: Mapped[datetime | None] = mapped_column(DateTime)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(
        SessionStatus, native_enum=False, name="session_status", create_constraint=True
    ),
        default=SessionStatus.ACTIVE,
    )
    # "line" or "dwell": how the vehicle left, which is a fact about the camera
    # view and changes how much to trust the duration.
    exit_reason: Mapped[str | None] = mapped_column(String(16))
    plate_confidence: Mapped[float | None] = mapped_column(Float)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    review_reason: Mapped[str | None] = mapped_column(String(64))
    anomaly_reason: Mapped[str | None] = mapped_column(String(64))

    # Three camera columns, so the join has to be named: `camera` is where the
    # visit belongs, not the (possibly different) gates it entered or exited.
    camera: Mapped[Camera] = relationship(foreign_keys=[camera_id])
    plate: Mapped[LicensePlate | None] = relationship()
    vehicle: Mapped[Vehicle | None] = relationship()
    slot: Mapped[ParkingSlot | None] = relationship()

    def __repr__(self) -> str:
        return (
            f"<Session {self.source_session_id} {self.status.value} "
            f"duration={self.duration_seconds}>"
        )


class SlotOccupancyHistory(Base):
    """One completed occupancy of one bay: FREE -> OCCUPIED -> FREE.

    Separate from sessions because a bay is occupied by plenty of vehicles the
    gate never saw, and this is what answers "how long was A3 taken today"
    without joining through a nullable plate. A row is written when the interval
    **closes**, so the currently-occupied bay has no history row yet - the live
    truth is `parking_slots.current_status`.
    """

    __tablename__ = "slot_occupancy_history"
    __table_args__ = (
        UniqueConstraint("slot_id", "started_at"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="interval_is_ordered"),
        Index("ix_slot_occupancy_history_slot_started", "slot_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("parking_slots.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("parking_sessions.id"))
    track_id: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    # What the containment score was when the bay committed - the evidence
    # behind the decision, so a wrong call can be traced to a bad calibration.
    score: Mapped[float | None] = mapped_column(Float)

    slot: Mapped[ParkingSlot] = relationship()

    def __repr__(self) -> str:
        return f"<Occupancy slot={self.slot_id} {self.started_at} -> {self.ended_at}>"


class DetectionLog(Base):
    """Meaningful domain events, one row per transition. Not a per-frame dump.

    Every event the bus carried is worth keeping only as a diagnostic trail: the
    payload is the same dict the event published, so a row here can be replayed
    against the pipeline's own output. Volume is bounded by the FSM - a bay that
    stays occupied emits nothing - which is why no retention job is needed yet.
    """

    __tablename__ = "detection_logs"
    __table_args__ = (
        Index("ix_detection_logs_camera_ts", "camera_id", "occurred_at"),
        Index("ix_detection_logs_event_type", "event_type"),
    )

    # bigint in the doc. SQLite only auto-increments INTEGER, so the variant is
    # what keeps this one definition valid on both databases.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    event_type: Mapped[str] = mapped_column(String(32))
    camera_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("cameras.camera_id"))
    track_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_class: Mapped[str | None] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # The visit this event belongs to, once it can be named. Keeps "what
    # happened to this car" one query instead of a time-range guess.
    session_id: Mapped[int | None] = mapped_column(ForeignKey("parking_sessions.id"))

    def __repr__(self) -> str:
        return f"<Log {self.event_type} {self.camera_id} @{self.occurred_at}>"


# The operations Phase 8 actually writes an audit row for. A vocabulary with no
# writer in it is drift waiting to happen, so this list only contains actions a
# router performs; a new audited endpoint adds a word here and in its migration.
AUDIT_ACTIONS = (
    "login",
    "login_failed",
    "logout",
    "user_created",
    "user_deactivated",
    "plate_corrected",
    "session_force_closed",
    "camera_registered",
    "camera_updated",
    "camera_restart",
    "slot_created",
    "slot_polygon_updated",
)


class User(Base, TimestampMixin):
    """An account that can call the API (Phase 8).

    `password_hash` is an Argon2id hash, not an encryption of the password, and
    no endpoint ever returns this column - the response models in `api/schemas.py`
    have no field for it. The doc's `email (UK)` is the natural key, stored
    lower-cased by `UserRepository` so `Saurabh@X.com` and `saurabh@x.com` cannot
    become two accounts with one password.

    Accounts are **deactivated**, never deleted: `AuditLog.actor_user_id` points
    here, and deleting a row would silently rewrite who did what.

    `tokens_invalid_before` is the logout epoch. A JWT is worthless on its own
    once issued - there is no server-side list to strike it off - so "sign me out"
    needs a place to say *everything issued before this instant is withdrawn*. It
    is compared against each token's `iat`, in naive UTC like every other column
    here (`db/base.py`), and stays NULL for an account that has never signed out.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        Enum(Role, native_enum=False, name="user_role", create_constraint=True)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tokens_invalid_before: Mapped[datetime | None] = mapped_column(DateTime)

    def __repr__(self) -> str:
        return f"<User {self.email} {self.role.value} active={self.is_active}>"


class AuditLog(Base):
    """Who changed what, appended to and never updated.

    `actor_email` is stored alongside the id on purpose. A failed login has no
    user row to point at, and a later deletion of an account must not turn its
    history into NULL - the doc calls this table append-only, and an append-only
    table that loses its subject when a row elsewhere disappears is not an audit
    trail.

    `detail` is the before/after the endpoint saw, with secrets and plate text
    removed by the caller: this column is read back by an ADMIN-facing list, so
    whatever is written here is disclosed.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(f"action IN {AUDIT_ACTIONS!r}", name="action_is_a_written_operation"),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor_email: Mapped[str | None] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(48))
    # A target is whatever the action named: a session id string, a camera id, a
    # slot code. Not a FK, because the row it points at may legitimately be gone
    # and an audit trail must not be the reason a delete fails.
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # The address that made the call, as the reverse proxy saw it. IPv6-sized.
    source_ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<Audit {self.action} actor={self.actor_email} target={self.target_id}>"


# Every table this project's migrations are supposed to have created. Read from
# the metadata rather than typed out again: a model added without its migration
# then shows up in `/health` as a missing table, which is the answer worth
# having, and the guard test in `tests/unit/test_db_schema.py` is what keeps the
# two lists equal in the first place.
MIGRATED_TABLES: tuple[str, ...] = tuple(sorted(Base.metadata.tables))
