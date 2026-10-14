"""Vehicle rows: identity that survives the sighting.

A ByteTrack id is a sighting, not a vehicle: it is reassigned whenever the
tracker loses a car, and the same number comes back for a different one. What
does survive is the plate, so the vehicle row is keyed on the plate plus the
class the detector reported for it - the most that can be claimed without
registration data, which arrives with accounts in a later phase.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from smart_parking.db.models import LicensePlate, Vehicle


class VehicleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, vehicle_id: int) -> Vehicle | None:
        return self.session.get(Vehicle, vehicle_id)

    def find(self, plate_id: int, vehicle_type: str | None) -> Vehicle | None:
        return self.session.scalar(
            select(Vehicle).where(
                Vehicle.plate_id == plate_id, Vehicle.vehicle_type == (vehicle_type or "car")
            )
        )

    def record(self, plate: LicensePlate, vehicle_type: str | None) -> Vehicle:
        """Find the vehicle behind this plate, or declare it for the first time.

        `vehicle_type` comes from the detection that carried the plate
        (`core.schemas.VEHICLE_CLASS_IDS`), so it is an observation of this
        vehicle rather than a label someone chose. An unknown class is stored as
        "car", which is what the detector reports for most vehicles at a gate
        and is honest about being the common case, not a guess at a specific one.
        """
        chosen = vehicle_type or "car"
        vehicle = self.find(plate.id, chosen)
        if vehicle is not None:
            return vehicle
        vehicle = Vehicle(plate_id=plate.id, vehicle_type=chosen)
        self.session.add(vehicle)
        self.session.flush()
        return vehicle

    def for_plate(self, plate: LicensePlate) -> list[Vehicle]:
        return list(
            self.session.scalars(
                select(Vehicle).where(Vehicle.plate_id == plate.id).order_by(Vehicle.vehicle_type)
            )
        )

    def all(self) -> list[Vehicle]:
        return list(self.session.scalars(select(Vehicle).order_by(Vehicle.id)))
