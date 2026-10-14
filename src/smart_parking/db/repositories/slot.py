"""Slot rows and their occupancy intervals.

`SlotRepository.sync` takes the same list of dicts the calibration tool writes
(`{code, polygon, type}`), so a bay in the database is the bay an operator
drew - not a second definition someone has to keep in step with the file.

Occupancy is split into two writes on purpose. `parking_slots.current_status`
is what the lot looks like now, and `slot_occupancy_history` is one closed
interval per FREE -> OCCUPIED -> FREE cycle. That is what keeps the write volume
sane: a bay occupied for an hour of video is one row, because the FSM only
emits on transitions.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from smart_parking.core.schemas import SlotStatus, SlotType
from smart_parking.db.models import ParkingSlot, SlotOccupancyHistory


class SlotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, slot_id: int) -> ParkingSlot | None:
        return self.session.get(ParkingSlot, slot_id)

    def find(self, camera_id: str, slot_code: str) -> ParkingSlot | None:
        return self.session.scalar(
            select(ParkingSlot).where(
                ParkingSlot.camera_id == camera_id, ParkingSlot.slot_code == slot_code
            )
        )

    def for_camera(self, camera_id: str) -> list[ParkingSlot]:
        return list(
            self.session.scalars(
                select(ParkingSlot).where(ParkingSlot.camera_id == camera_id).order_by(ParkingSlot.slot_code)
            )
        )

    def sync(
        self,
        camera_id: str,
        slots: list[dict],
        frame_size: dict | None = None,
    ) -> list[ParkingSlot]:
        """Create or refresh one row per calibrated bay. Returns every row.

        Geometry is rewritten from the calibration file each time; `status_since`
        and the occupancy rows are not touched, because those are observations,
        not configuration. A bay deleted from the drawing is left alone rather
        than deleted, so its history survives.
        """
        out = []
        for entry in slots:
            code = entry["code"]
            slot = self.find(camera_id, code)
            if slot is None:
                slot = ParkingSlot(
                    camera_id=camera_id,
                    slot_code=code,
                    current_status=SlotStatus.UNKNOWN,
                )
                self.session.add(slot)
            slot.polygon = entry.get("polygon", [])
            slot.slot_type = SlotType(entry.get("type", SlotType.REGULAR.value))
            slot.frame_size = frame_size
            self.session.flush()
            out.append(slot)
        return out

    def set_status(
        self,
        slot: ParkingSlot,
        status: SlotStatus,
        since: datetime,
        track_id: int | None = None,
    ) -> bool:
        """Write a committed transition. Returns False when nothing changed.

        UNKNOWN is always written even when the row already says UNKNOWN: a
        camera that stopped reporting is a fact an operator needs to see, and
        the FSM deliberately re-announces it.
        """
        if slot.current_status is status and status is not SlotStatus.UNKNOWN:
            return False
        slot.current_status = status
        slot.status_since = since
        slot.occupied_by_track_id = track_id if status is SlotStatus.OCCUPIED else None
        self.session.flush()
        return True

    def set_polygon(
        self, slot: ParkingSlot, polygon: list[list[int]], frame_size: dict | None
    ) -> ParkingSlot:
        """Replace a bay's shape, and the frame size it was drawn on.

        The calibration tool writes this after a person moves the corners; a
        polygon without the size it was measured in is a shape nobody can
        rescale onto a different camera correctly.
        """
        slot.polygon = polygon
        if frame_size is not None:
            slot.frame_size = frame_size
        self.session.flush()
        return slot

    def status_counts(self, camera_id: str | None = None) -> dict[SlotStatus, int]:
        """How many bays are in each committed state, without loading geometry.

        A dashboard polling `/lots/{id}/summary` should not pay for every polygon
        in the lot to get four numbers back.
        """
        stmt = select(ParkingSlot.current_status, func.count()).group_by(
            ParkingSlot.current_status
        )
        if camera_id is not None:
            stmt = stmt.where(ParkingSlot.camera_id == camera_id)
        return {status: count for status, count in self.session.execute(stmt)}

    def filtered(
        self,
        *,
        camera_id: str | None = None,
        status: SlotStatus | None = None,
        slot_type: SlotType | None = None,
    ) -> list[ParkingSlot]:
        stmt = select(ParkingSlot)
        if camera_id is not None:
            stmt = stmt.where(ParkingSlot.camera_id == camera_id)
        if status is not None:
            stmt = stmt.where(ParkingSlot.current_status == status)
        if slot_type is not None:
            stmt = stmt.where(ParkingSlot.slot_type == slot_type)
        stmt = stmt.order_by(ParkingSlot.camera_id, ParkingSlot.slot_code)
        return list(self.session.scalars(stmt))


class OccupancyRepository:
    """One interval per stay: opened when a bay commits, closed when it frees."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def open_interval(
        self,
        slot: ParkingSlot,
        started_at: datetime,
        track_id: int | None = None,
        score: float | None = None,
        session_id: int | None = None,
    ) -> SlotOccupancyHistory:
        row = SlotOccupancyHistory(
            slot_id=slot.id,
            started_at=started_at,
            track_id=track_id,
            score=score,
            session_id=session_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def open_for(self, slot_id: int) -> SlotOccupancyHistory | None:
        """The interval still running for this bay, if any.

        `ended_at IS NULL` means "still occupied right now", which is a live
        fact rather than history - which is exactly why it is not closed by a
        timer here, but by the FSM's own FREE transition.
        """
        return self.session.scalar(
            select(SlotOccupancyHistory)
            .where(
                SlotOccupancyHistory.slot_id == slot_id,
                SlotOccupancyHistory.ended_at.is_(None),
            )
            .order_by(SlotOccupancyHistory.started_at.desc())
        )

    def close_interval(
        self, slot_id: int, ended_at: datetime, session_id: int | None = None
    ) -> SlotOccupancyHistory | None:
        interval = self.open_for(slot_id)
        if interval is None:
            return None
        interval.ended_at = ended_at
        if session_id is not None:
            interval.session_id = session_id
        self.session.flush()
        return interval

    def history(self, slot_id: int) -> list[SlotOccupancyHistory]:
        return list(
            self.session.scalars(
                select(SlotOccupancyHistory)
                .where(SlotOccupancyHistory.slot_id == slot_id)
                .order_by(SlotOccupancyHistory.started_at)
            )
        )

    def intervals_between(
        self,
        from_ts: datetime,
        to_ts: datetime,
        camera_id: str | None = None,
    ) -> list[tuple[SlotOccupancyHistory, ParkingSlot]]:
        """Every interval that *overlapped* the window, with its bay.

        Overlap, not "started in": a vehicle that arrived at 23:50 and left at
        00:20 was occupying a bay through the hour boundary, and a series that
        only counts arrivals makes it invisible. The join is here so the caller
        gets the bay (and so its camera) without a lazy load per row - the
        grouping the API does afterwards is arithmetic, not SQL.
        """
        stmt = (
            select(SlotOccupancyHistory, ParkingSlot)
            .join(ParkingSlot, ParkingSlot.id == SlotOccupancyHistory.slot_id)
            .where(
                SlotOccupancyHistory.started_at <= to_ts,
                # An interval still running (NULL end) overlaps every window that
                # reached its start.
                or_(
                    SlotOccupancyHistory.ended_at.is_(None),
                    SlotOccupancyHistory.ended_at >= from_ts,
                ),
            )
            .order_by(SlotOccupancyHistory.started_at)
        )
        if camera_id is not None:
            stmt = stmt.where(ParkingSlot.camera_id == camera_id)
        return [(interval, slot) for interval, slot in self.session.execute(stmt)]
