"""Replay of labelled vehicle boxes.

A rendered fixture has no real cars in it, so the detector finds nothing and the
stages behind it cannot be exercised at all. A box file whose rows say
``{frame, track_id, bbox}`` hands a pipeline the same ``Track`` objects the
tracker would have produced - for the frames it did not produce.

Used by the Phase 5 (plate reading) and Phase 6 (gate sessions) CLIs and by the
API's replay cameras (Phase 8), so a fixture file is read exactly one way.

The confidence is 1.0 because the box is given, not inferred: a 0.8 in that field
would look like a measurement of a model that was never run.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from smart_parking.core.schemas import Track

if TYPE_CHECKING:  # only for the signature: replay must not import the config layer
    from smart_parking.core.config import AppConfig


def tracks_by_frame(rows: list[dict], footprint_ratio: float = 0.4) -> dict[int, list[Track]]:
    """Group a labelled box file by the frame each box was recorded on."""
    out: dict[int, list[Track]] = defaultdict(list)
    for row in rows:
        x1, y1, x2, y2 = (float(v) for v in row["bbox"])
        out[int(row["frame"])].append(
            Track(
                bbox=(x1, y1, x2, y2),
                confidence=1.0,
                class_id=int(row.get("class_id", 2)),
                class_name=row.get("class_name", "car"),
                track_id=int(row["track_id"]),
                footprint_ratio=footprint_ratio,
            )
        )
    return dict(out)


def load_track_file(path: str | Path, cfg: AppConfig) -> tuple[dict[int, list[Track]], list[dict]]:
    """Read a box file into `(frames, expectations)`.

    Lives here rather than in each caller because the API's replay cameras and the
    gate CLI's `--tracks` have to agree on the file's two shapes - a bare list of
    rows, or `{"tracks": [...], "expectations": [...]}` - and on resolving a
    relative path against the project root instead of the working directory.
    """
    p = cfg.resolve(Path(path))
    payload = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("tracks", [])
        expectations = payload.get("expectations", [])
    else:
        rows, expectations = payload, []
    return tracks_by_frame(rows, cfg.occupancy.footprint_ratio), expectations
