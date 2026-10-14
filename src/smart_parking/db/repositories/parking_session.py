"""Parking session rows: the durable copy of a Phase 6 visit.

The in-memory state machine in `services/session_service.py` stays the authority
on what a visit *is*; this repository is the projection of that decision into a
row. Two rules follow from that and are enforced here:

* a duplicate write of the same visit is an update of the same row, because the
  key is the id the state machine minted (`source_session_id`), not a timestamp
  that could drift;
* a visit that never had an entry measured keeps `entry_time = NULL`. The row
  records the half that the camera actually saw, which is the whole point of
  calling it an anomaly instead of a completed session.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart_parking.core.schemas import DUPLICATE_PLATE, SessionStatus
from smart_parking.db.models import LicensePlate, ParkingSession, Vehicle


class ParkingSessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- reading ------------------------------------------------------------
    def get(self, source_session_id: str) -> ParkingSession | None:
        return self.session.scalar(
            select(ParkingSession).where(ParkingSession.source_session_id == source_session_id)
        )

    def by_id(self, row_id: int) -> ParkingSession | None:
        return self.session.get(ParkingSession, row_id)

    def active_for(self, camera_id: str, track_id: int) -> ParkingSession | None:
        return self.session.scalar(
            select(ParkingSession).where(
                ParkingSession.camera_id == camera_id,
                ParkingSession.entry_track_id == track_id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
        )

    def active_for_plate(self, plate_id: int) -> ParkingSession | None:
        return self.session.scalar(
            select(ParkingSession).where(
                ParkingSession.plate_id == plate_id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
        )

    def active_for_slot(self, slot_id: int) -> ParkingSession | None:
        return self.session.scalar(
            select(ParkingSession).where(
                ParkingSession.slot_id == slot_id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
        )

    def all(self) -> list[ParkingSession]:
        return list(
            self.session.scalars(select(ParkingSession).order_by(ParkingSession.id))
        )

    def _filtered_stmt(
        self,
        *,
        status: SessionStatus | None,
        camera_id: str | None,
        plate_id: int | None,
        from_ts: datetime | None,
        to_ts: datetime | None,
    ):
        """The one WHERE clause shared by the page and its total.

        Two implementations of a filter - one for the rows, one for the count -
        is how a paginated list starts disagreeing with itself about how many
        sessions matched.
        """
        stmt = select(ParkingSession)
        if status is not None:
            stmt = stmt.where(ParkingSession.status == status)
        if camera_id is not None:
            stmt = stmt.where(ParkingSession.camera_id == camera_id)
        if plate_id is not None:
            stmt = stmt.where(ParkingSession.plate_id == plate_id)
        # The window is over *entry* time: "what happened between 9 and 10" is a
        # question about arrivals, and a session that straddles the end of the
        # window is still one that started inside it.
        if from_ts is not None:
            stmt = stmt.where(ParkingSession.entry_time >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(ParkingSession.entry_time <= to_ts)
        return stmt

    def filtered(
        self,
        *,
        status: SessionStatus | None = None,
        camera_id: str | None = None,
        plate_id: int | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ParkingSession]:
        stmt = (
            self._filtered_stmt(
                status=status, camera_id=camera_id, plate_id=plate_id, from_ts=from_ts, to_ts=to_ts
            )
            .order_by(ParkingSession.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def count_filtered(
        self,
        *,
        status: SessionStatus | None = None,
        camera_id: str | None = None,
        plate_id: int | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
    ) -> int:
        stmt = self._filtered_stmt(
            status=status, camera_id=camera_id, plate_id=plate_id, from_ts=from_ts, to_ts=to_ts
        ).subquery()
        return int(self.session.scalar(select(func.count()).select_from(stmt)) or 0)

    def active_slot_map(self) -> dict[int, str]:
        """Bay id -> the live session holding it, in one query.

        The slot list wants this per row, and a route that asked for it per row
        would be one SELECT per bay on a lot an operator is watching every second.
        """
        rows = self.session.execute(
            select(ParkingSession.slot_id, ParkingSession.source_session_id).where(
                ParkingSession.status == SessionStatus.ACTIVE,
                ParkingSession.slot_id.is_not(None),
            )
        )
        return {slot_id: source_id for slot_id, source_id in rows}

    def entry_times(self, from_ts: datetime | None = None, to_ts: datetime | None = None) -> list[datetime]:
        """Every measured arrival, for the hour-of-day histogram.

        Only rows with a real `entry_time`: an orphan exit has no arrival, and
        counting it as one would put a car in an hour nobody saw it.
        """
        stmt = select(ParkingSession.entry_time).where(ParkingSession.entry_time.is_not(None))
        if from_ts is not None:
            stmt = stmt.where(ParkingSession.entry_time >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(ParkingSession.entry_time <= to_ts)
        return list(self.session.scalars(stmt.order_by(ParkingSession.entry_time)))

    def durations(
        self, from_ts: datetime | None = None, to_ts: datetime | None = None
    ) -> list[float]:
        """Every duration the gate actually measured in the window, for aggregates.

        NULLs are excluded by the query, not by the caller, so "average stay"
        means "over the visits that have a length" - an orphan exit row with no
        entry can only contribute a null, and treating that as a zero would pull
        the average down with a number that was never observed.
        """
        stmt = select(ParkingSession.duration_seconds).where(
            ParkingSession.duration_seconds.is_not(None)
        )
        if from_ts is not None:
            stmt = stmt.where(ParkingSession.entry_time >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(ParkingSession.entry_time <= to_ts)
        return list(self.session.scalars(stmt))

    def count_by_status(self) -> dict[SessionStatus, int]:
        stmt = (
            select(ParkingSession.status, func.count()).group_by(ParkingSession.status)
        )
        return {status: count for status, count in self.session.execute(stmt)}

    # -- writing ------------------------------------------------------------
    def open(
        self,
        source_session_id: str,
        camera_id: str,
        track_id: int,
        entry_time: datetime,
        *,
        plate: LicensePlate | None = None,
        vehicle: Vehicle | None = None,
        ocr_confidence: float | None = None,
        plate_confidence: float | None = None,
        review_reason: str | None = None,
        anomaly_reason: str | None = None,
        status: SessionStatus | None = None,
    ) -> ParkingSession:
        """Open a visit. Re-opening the same id returns the row it already wrote.

        `status` is the domain's own verdict, passed through rather than
        re-derived: the state machine already decided whether this entry is an
        anomaly (a second live session for one plate), and a repository that
        second-guessed it would be a second authority on the same fact.

        A *different* id trying to be ACTIVE for the same camera and track hits
        the partial unique index and raises `IntegrityError`. That is the point:
        the tracker's id is not a booking reference, and the database must not
        accept two live visits for one vehicle in view.
        """
        existing = self.get(source_session_id)
        if existing is not None:
            return existing
        row = ParkingSession(
            source_session_id=source_session_id,
            camera_id=camera_id,
            entry_track_id=track_id,
            entry_camera_id=camera_id,
            entry_time=entry_time,
            status=status or (SessionStatus.ANOMALY if anomaly_reason else SessionStatus.ACTIVE),
            plate_id=None if plate is None else plate.id,
            vehicle_id=None if vehicle is None else vehicle.id,
            ocr_confidence=ocr_confidence,
            plate_confidence=plate_confidence,
            review_reason=review_reason,
            anomaly_reason=anomaly_reason,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_orphan_exit(
        self,
        source_session_id: str,
        camera_id: str,
        track_id: int,
        exit_time: datetime,
        exit_reason: str,
        anomaly_reason: str,
        *,
        plate: LicensePlate | None = None,
        ocr_confidence: float | None = None,
    ) -> ParkingSession:
        """An exit with no entry: a row with no `entry_time` and no duration.

        Deliberately not `open()` followed by `close()`: that shape would have
        to put *something* in `entry_time`, and the only something available is
        a guess. `duration_seconds` stays NULL so nothing downstream can bill,
        average or report a visit length that was never observed.
        """
        existing = self.get(source_session_id)
        if existing is not None:
            return existing
        row = ParkingSession(
            source_session_id=source_session_id,
            camera_id=camera_id,
            entry_track_id=track_id,
            exit_camera_id=camera_id,
            exit_time=exit_time,
            exit_reason=exit_reason,
            exit_track_id=track_id,
            duration_seconds=None,
            entry_time=None,
            status=SessionStatus.ANOMALY,
            anomaly_reason=anomaly_reason,
            plate_id=None if plate is None else plate.id,
            ocr_confidence=ocr_confidence,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def close(
        self,
        source_session_id: str,
        exit_time: datetime,
        duration_seconds: float | None,
        exit_reason: str | None,
        *,
        status: SessionStatus = SessionStatus.COMPLETED,
        exit_track_id: int | None = None,
        exit_camera_id: str | None = None,
        anomaly_reason: str | None = None,
        review_reason: str | None = None,
    ) -> ParkingSession:
        """Close an open visit and store the duration the domain measured.

        `duration_seconds` is written from the caller, never recomputed here: the
        state machine timed the visit in video time, and recomputing it from two
        datetimes would quietly redefine what a duration means if the two ever
        disagreed.
        """
        row = self.get(source_session_id)
        if row is None:
            raise ValueError(f"no session {source_session_id} to close")
        if row.exit_time is not None and row.exit_time != exit_time:
            # A second, different exit for one visit is a bug upstream, not a
            # fact to reconcile. Overwriting a measured time would hide it.
            raise ValueError(
                f"session {source_session_id} already closed at {row.exit_time}, "
                f"refusing a second exit at {exit_time}"
            )
        row.exit_time = exit_time
        row.duration_seconds = duration_seconds
        row.exit_reason = exit_reason
        row.exit_track_id = exit_track_id if exit_track_id is not None else row.exit_track_id
        row.exit_camera_id = exit_camera_id or row.exit_camera_id or row.camera_id
        row.status = status
        if anomaly_reason is not None:
            row.anomaly_reason = anomaly_reason
        if review_reason is not None:
            row.review_reason = review_reason
        self.session.flush()
        return row

    def bind_plate(
        self,
        row: ParkingSession,
        plate: LicensePlate,
        *,
        ocr_confidence: float | None = None,
        plate_confidence: float | None = None,
        vehicle: Vehicle | None = None,
    ) -> ParkingSession:
        """Attach a plate to a visit - and say so when the plate changed.

        A session that already carries a different plate is not silently
        rewritten: the domain flags a mid-visit plate change for review, and so
        does the row, because "which car was this" is the question an operator
        will ask about exactly these sessions.

        A plate some *other* live visit already holds is the row-level form of
        the domain's duplicate-session anomaly. The index would reject the write
        outright, which is the right refusal but the wrong outcome - the domain's
        rule is that both visits are kept and the newcomer is flagged. So the
        newcomer stops being ACTIVE here, in the same write, and the index is
        satisfied because there is only one live claim left.
        """
        if row.plate_id is not None and row.plate_id != plate.id:
            row.review_reason = row.review_reason or "plate changed"
        if row.status is SessionStatus.ACTIVE:
            clash = self.active_for_plate(plate.id)
            if clash is not None and clash.id != row.id:
                row.status = SessionStatus.ANOMALY
                row.anomaly_reason = row.anomaly_reason or DUPLICATE_PLATE
        row.plate_id = plate.id
        if ocr_confidence is not None:
            row.ocr_confidence = ocr_confidence
        if plate_confidence is not None:
            row.plate_confidence = plate_confidence
        if vehicle is not None:
            row.vehicle_id = vehicle.id
        self.session.flush()
        # The caller usually read `row.plate` before the write - an endpoint
        # correcting a misread plate wants the old text for its audit row - and
        # that loaded relationship would otherwise answer with the value this
        # method just replaced. Expiring it makes the next read the new one.
        self.session.expire(row, ["plate"])
        if vehicle is not None:
            self.session.expire(row, ["vehicle"])
        return row

    def bind_slot(self, row: ParkingSession, slot_id: int) -> ParkingSession:
        """Link a visit to a bay. Optional, and still uncalled by the pipeline.

        The gate camera does not see the bays, so nothing measures this yet;
        the column and its one-ACTIVE-per-slot index exist so the link is a
        later phase's wiring job rather than a schema change.
        """
        row.slot_id = slot_id
        self.session.flush()
        return row

    def set_status(self, row: ParkingSession, status: SessionStatus, reason: str | None = None) -> ParkingSession:
        row.status = status
        if reason is not None:
            if status is SessionStatus.ANOMALY:
                row.anomaly_reason = reason
            else:
                row.review_reason = reason
        self.session.flush()
        return row
