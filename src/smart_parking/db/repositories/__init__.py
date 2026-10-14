"""Repositories: the only place SQL is written.

Two conventions run through every module here, and the pipeline depends on both:

* A repository takes an `orm.Session` and never commits. The caller owns the unit
  of work (`db.session.session_scope`, or whatever `services/persistence.py`
  builds on it), so several repository calls become one atomic write and a
  failure rolls all of them back instead of leaving half a session on disk.
* Repositories speak datetimes and rows, not the domain's epoch floats. The
  conversion happens once, at the seam in `services/persistence.py`, so nothing
  under this package needs to know that the pipeline keeps time as seconds.

`Repositories` bundles them over one session so a caller passes the
bundle around rather than seven arguments; `normalise` is the Phase 5 validator's
own plate normaliser, required because a database-side second rule would be free
to disagree with the voting stage that produced the plate.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from smart_parking.db.repositories.audit import AuditLogRepository
from smart_parking.db.repositories.camera import CameraRepository
from smart_parking.db.repositories.event_log import EventLogRepository
from smart_parking.db.repositories.parking_session import ParkingSessionRepository
from smart_parking.db.repositories.plate import PlateRepository
from smart_parking.db.repositories.slot import OccupancyRepository, SlotRepository
from smart_parking.db.repositories.user import UserRepository
from smart_parking.db.repositories.vehicle import VehicleRepository


class Repositories:
    def __init__(self, session: Session, *, normalise: Callable[[str], str]) -> None:
        self.session = session
        self.cameras = CameraRepository(session)
        self.slots = SlotRepository(session)
        self.occupancy = OccupancyRepository(session)
        self.plates = PlateRepository(session, normalise)
        self.vehicles = VehicleRepository(session)
        self.sessions = ParkingSessionRepository(session)
        self.events = EventLogRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditLogRepository(session)


__all__ = [
    "AuditLogRepository",
    "CameraRepository",
    "EventLogRepository",
    "OccupancyRepository",
    "ParkingSessionRepository",
    "PlateRepository",
    "Repositories",
    "SlotRepository",
    "UserRepository",
    "VehicleRepository",
]
