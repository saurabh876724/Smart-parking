"""The event log: one row per meaningful domain event, not per frame.

What gets written is exactly what the bus carried, so a row can be checked
against the pipeline's own console output. The volume is bounded by the state
machines rather than by a retention job: the slot FSM emits on transitions only,
and the session machine emits one entry and one exit per visit, so an hour of
quiet video costs a handful of rows.

`SUMMARY` events are skipped by default. They are a per-frame counter snapshot -
useful to a WebSocket client, and a per-frame dump if written here, which is the
one thing this table is documented as not being.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart_parking.core.schemas import Event, SUMMARY
from smart_parking.db.base import to_utc
from smart_parking.db.models import DetectionLog


class EventLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        event: Event,
        *,
        session_row_id: int | None = None,
        vehicle_class: str | None = None,
        skip_types: tuple[str, ...] = (SUMMARY,),
    ) -> DetectionLog | None:
        """Store one event. Returns None when the event type is not loggable."""
        if event.type in skip_types:
            return None
        row = DetectionLog(
            event_type=event.type,
            camera_id=event.camera_id,
            track_id=event.payload.get("track_id"),
            vehicle_class=vehicle_class or event.payload.get("class"),
            occurred_at=to_utc(event.timestamp),
            payload=event.payload,
            session_id=session_row_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def for_camera(self, camera_id: str, limit: int = 50) -> list[DetectionLog]:
        return list(
            self.session.scalars(
                select(DetectionLog)
                .where(DetectionLog.camera_id == camera_id)
                .order_by(DetectionLog.id.desc())
                .limit(limit)
            )
        )

    def for_session(self, session_row_id: int) -> list[DetectionLog]:
        return list(
            self.session.scalars(
                select(DetectionLog)
                .where(DetectionLog.session_id == session_row_id)
                .order_by(DetectionLog.id)
            )
        )

    def count_by_type(self) -> dict[str, int]:
        rows = self.session.execute(
            select(DetectionLog.event_type, func.count()).group_by(DetectionLog.event_type)
        ).all()
        return {event_type: total for event_type, total in rows}

    def total(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(DetectionLog)) or 0)
