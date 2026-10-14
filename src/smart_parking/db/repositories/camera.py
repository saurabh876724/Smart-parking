"""Camera rows: the persisted mirror of cameras.yaml."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from smart_parking.core.config import CameraConfig
from smart_parking.db.models import Camera


class CameraRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, camera_id: str) -> Camera | None:
        return self.session.get(Camera, camera_id)

    def get_or_create(self, camera_id: str, **fields) -> Camera:
        camera = self.session.get(Camera, camera_id)
        if camera is not None:
            return camera
        camera = Camera(camera_id=camera_id, **fields)
        self.session.add(camera)
        # Flush so a caller can read the row (and point a foreign key at it)
        # before the transaction commits, without committing on their behalf.
        self.session.flush()
        return camera

    def upsert(self, camera_id: str, **fields) -> Camera:
        """Create, or overwrite the reference fields of an existing row.

        Called at startup from the live config, so a camera whose source or
        calibration file moved is reflected rather than duplicated.
        """
        camera = self.session.get(Camera, camera_id)
        if camera is None:
            return self.get_or_create(camera_id, **fields)
        for key, value in fields.items():
            setattr(camera, key, value)
        self.session.flush()
        return camera

    def update(self, row: Camera, **fields) -> Camera:
        """Assign only the fields that were named, then flush.

        `upsert` is the startup path and overwrites the reference fields from the
        live config; this is an operator's PATCH, where a field the request left
        out has to keep the value it already had.
        """
        for key, value in fields.items():
            setattr(row, key, value)
        self.session.flush()
        return row

    def sync_from_config(self, cameras: list[CameraConfig]) -> list[Camera]:
        """Every configured camera, using the project's own identifiers."""
        return [
            self.upsert(
                camera.id,
                name=camera.name,
                role=camera.role,
                source=camera.source,
                slots_file=camera.slots_file,
                gate_file=camera.gate_file,
                fps_limit=camera.fps_limit,
            )
            for camera in cameras
        ]

    def mark_seen(self, camera_id: str, when: datetime) -> Camera | None:
        camera = self.session.get(Camera, camera_id)
        if camera is None:
            return None
        camera.last_seen_at = when
        self.session.flush()
        return camera

    def all(self) -> list[Camera]:
        return list(self.session.scalars(select(Camera).order_by(Camera.camera_id)))
