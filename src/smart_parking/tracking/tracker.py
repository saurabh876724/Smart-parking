"""Tracking behind a switch.

Ultralytics ships ByteTrack, so enabling it is a config flag rather than a
dependency. Phase 3 runs with it off (plain per-frame detections, as the doc
prescribes); flipping it on is Phase 4.
"""

from __future__ import annotations

from ultralytics import YOLO

from smart_parking.core.config import DetectionConfig
from smart_parking.core.logging import get_logger
from smart_parking.core.schemas import VEHICLE_CLASS_IDS, Track
from smart_parking.detection.vehicle_detector import precision_kwargs, resolve_device

log = get_logger("tracking")


class Tracker:
    """Wraps model.track(), converting results into Track objects.

    ``enabled=False`` yields synthetic per-frame ids. Nothing above may treat
    those as identity - the occupancy FSM switches its dwell-reset rule off when
    tracking is disabled - so never bind a session to them.
    """

    def __init__(
        self,
        model: YOLO,
        cfg: DetectionConfig,
        enabled: bool = True,
        tracker: str = "bytetrack.yaml",
        footprint_ratio: float = 0.4,
    ) -> None:
        self.model = model
        self.cfg = cfg
        self.enabled = enabled
        self.tracker = tracker
        self.footprint_ratio = footprint_ratio
        self.device = resolve_device(cfg.device)
        # Ultralytics only enables FP16 on CUDA; asking for it on CPU errors out.
        self.half = bool(cfg.half and self.device.startswith("cuda"))
        self._next_id = 1

    def update(self, frame) -> list[Track]:
        common = dict(
            conf=self.cfg.confidence,
            iou=0.5,
            imgsz=self.cfg.imgsz,
            classes=list(self.cfg.classes),
            device=self.device,
            verbose=False,
            **precision_kwargs(self.half),
        )
        if self.enabled:
            results = self.model.track(frame, tracker=self.tracker, persist=True, **common)
        else:
            results = self.model.predict(frame, **common)

        tracks: list[Track] = []
        for res in results:
            boxes = res.boxes
            if boxes is None or len(boxes) == 0:
                continue
            ids = boxes.id
            for i, box in enumerate(boxes):
                cls_id = int(box.cls[0])
                x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
                if self.enabled and ids is not None:
                    track_id = int(ids[i])
                else:
                    track_id = self._next_id
                    self._next_id += 1
                tracks.append(
                    Track(
                        bbox=(x1, y1, x2, y2),
                        confidence=float(box.conf[0]),
                        class_id=cls_id,
                        class_name=VEHICLE_CLASS_IDS.get(cls_id, f"class_{cls_id}"),
                        track_id=track_id,
                        footprint_ratio=self.footprint_ratio,
                    )
                )
        return tracks

    def reset(self) -> None:
        """Call on camera reconnect - ByteTrack state is stale after a gap."""
        self.model.predictor = None
        self._next_id = 1
