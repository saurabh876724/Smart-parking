"""Plate rows: one per distinct normalised registration number.

The normaliser is injected rather than imported. It is the Phase 5 validator's
own (`anpr/validation.py`), and that stage is the only thing in the project
entitled to decide what `mh-20-ab-1234` and `MH20AB1234` mean - a second
normalisation rule in the database layer would be free to disagree with the
voting stage that produced the plate, which is how one car ends up as two.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart_parking.db.models import LicensePlate


class PlateRepository:
    def __init__(self, session: Session, normalise: Callable[[str], str]) -> None:
        self.session = session
        self.normalise = normalise

    def find(self, plate_text: str) -> LicensePlate | None:
        """Lookup by the exact text; callers pass what they already normalised."""
        return self.session.scalar(
            select(LicensePlate).where(LicensePlate.plate_text == self.normalise(plate_text))
        )

    def get(self, plate_id: int) -> LicensePlate | None:
        return self.session.get(LicensePlate, plate_id)

    def record(
        self,
        plate_text: str,
        seen_at: datetime,
        confidence: float | None = None,
    ) -> LicensePlate:
        """Find or create a plate and note this sighting of it.

        `confidence` keeps the **best** score ever seen rather than the latest:
        a plate read once at 0.99 should not be reported as a 0.6 plate because
        a later frame was blurrier. `reads` counts sightings, not votes.
        """
        text = self.normalise(plate_text)
        plate = self.session.scalar(
            select(LicensePlate).where(LicensePlate.plate_text == text)
        )
        if plate is None:
            plate = LicensePlate(
                plate_text=text,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
                confidence=confidence,
                reads=1,
            )
            self.session.add(plate)
        else:
            if plate.first_seen_at is None or seen_at < plate.first_seen_at:
                plate.first_seen_at = seen_at
            if plate.last_seen_at is None or seen_at > plate.last_seen_at:
                plate.last_seen_at = seen_at
            if confidence is not None and (plate.confidence is None or confidence > plate.confidence):
                plate.confidence = confidence
            plate.reads += 1
        self.session.flush()
        return plate

    def search(
        self, fragment: str, *, limit: int = 50, offset: int = 0, total: bool = True
    ) -> tuple[list[LicensePlate], int]:
        """Substring match on the normalised text, plus how many rows matched.

        The fragment goes through the same normaliser as the stored text, so
        searching `mh20ab1234`, `MH 20 AB 1234` and `mh-20-ab-12` behaves the
        same way the reader that produced the plate would behave. `LIKE` is used
        with an escape character because a plate search that treats `%` as a
        wildcard is a way to dump the table by typing one key.
        """
        text = self.normalise(fragment)
        escaped = re.sub(r"[%_]", lambda m: f"\\{m.group(0)}", text)
        pattern = f"%{escaped}%"
        where = LicensePlate.plate_text.like(pattern, escape="\\")
        rows = list(
            self.session.scalars(
                select(LicensePlate).where(where).order_by(LicensePlate.plate_text).limit(limit).offset(offset)
            )
        )
        count = (
            int(self.session.scalar(select(func.count()).select_from(LicensePlate).where(where)) or 0)
            if total
            else len(rows)
        )
        return rows, count

    def total(self) -> int:
        return int(self.session.scalar(select(func.count(LicensePlate.id))) or 0)

    def all(self) -> list[LicensePlate]:
        return list(self.session.scalars(select(LicensePlate).order_by(LicensePlate.plate_text)))
