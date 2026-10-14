"""Phase 3: live slot occupancy with the debounce FSM.

    python scripts/run_occupancy.py                       # headless over the demo clip
    python scripts/run_occupancy.py --show                # operator window
    python scripts/run_occupancy.py --save-video out.mp4  # annotated recording
    python scripts/run_occupancy.py --persist             # write bays and stays to SQLite

`--persist` is Phase 7's addition: the occupancy events printed above are also
handed to the persistence worker, which writes them on its own thread so the frame
loop never waits for the database. A bay's committed transition is one row update
plus one interval per stay, never one row per frame.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import inspect  # noqa: E402

from smart_parking.core.config import (  # noqa: E402
    CameraConfig,
    DatabaseConfig,
    load_config,
    load_slots,
)
from smart_parking.core.events import RecordingEventBus  # noqa: E402
from smart_parking.core.logging import get_logger  # noqa: E402
from smart_parking.core.schemas import SLOT_FREED, SLOT_OCCUPIED, VIOLATION  # noqa: E402
from smart_parking.db.repositories import Repositories  # noqa: E402
from smart_parking.db.session import Database  # noqa: E402
from smart_parking.parking.geometry import slots_from_config  # noqa: E402
from smart_parking.pipeline.lot_pipeline import LotPipeline  # noqa: E402
from smart_parking.services.persistence import (  # noqa: E402
    PersistenceWorker,
    format_failures,
    plate_normaliser_for,
)

log = get_logger("run_occupancy")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", default="lot_cam1", help="id from configs/cameras.yaml")
    ap.add_argument("--source", default=None, help="override the camera source")
    ap.add_argument("--slots", default=None, help="override the slots file")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--save-video", default=None)
    ap.add_argument("--save-frames", default=None)
    ap.add_argument("--stride", type=int, default=None, help="run inference every Nth frame")
    ap.add_argument("--no-track", action="store_true", help="disable ByteTrack (Phase 3 plain overlap)")
    ap.add_argument("--loop", action="store_true", help="replay a file source forever (soak testing)")
    ap.add_argument(
        "--persist",
        action="store_true",
        help="write the bay states and stays this run produces to SQLite (Phase 7)",
    )
    ap.add_argument(
        "--db",
        default=None,
        help="database URL for --persist instead of database.url in configs/app.yaml",
    )
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    camera = next((c for c in cfg.cameras if c.id == args.camera), None)
    if camera is None:
        camera = CameraConfig(id=args.camera, name=args.camera, source=args.source or "", slots_file=args.slots)
    if args.source:
        camera.source = args.source
    if args.slots:
        camera.slots_file = args.slots
    if not camera.source:
        log.error("no source for camera %s", camera.id)
        return 2

    source = str(ROOT / camera.source) if not Path(camera.source).is_absolute() else camera.source
    if not camera.slots_file:
        log.error("camera %s has no slots_file - run scripts/calibrate.py first", camera.id)
        return 2

    slots = slots_from_config(load_slots(ROOT / camera.slots_file, ROOT), camera.id)

    bus = RecordingEventBus()
    pipeline = LotPipeline(cfg, camera, slots, bus=bus, track=not args.no_track, loop=args.loop)

    def log_event(event):
        if event.type in (SLOT_OCCUPIED, SLOT_FREED):
            p = event.payload
            print(f"  {p['slot_id']:>4} -> {p['status']:<16} track={p['track_id']} score={p['score']}")
        elif event.type == VIOLATION:
            print(f"  VIOLATION {event.payload}")

    bus.subscribe(log_event)

    print(f"\ncamera={camera.id}  slots={len(slots)}  source={source}\n" + "-" * 60)

    worker = None
    if args.persist:
        # The database holds the bays this run is using, so its camera row gets the
        # `--slots` override too, and a camera id that cameras.yaml does not
        # declare still has to exist before a bay event points at it.
        declared = list(cfg.cameras)
        if any(c.id == camera.id for c in declared):
            declared = [camera if c.id == camera.id else c for c in declared]
        else:
            declared.append(camera)
        run_cfg = cfg.model_copy(update={"cameras": declared})
        database = Database.from_config(
            run_cfg, DatabaseConfig(url=args.db) if args.db else run_cfg.database
        )
        if not inspect(database.engine).has_table("parking_sessions"):
            # The schema is the migrations' job, not a run's.
            log.error(
                "%s has no tables - run `python scripts/migrate.py upgrade head` first",
                database.url,
            )
            return 2
        worker = PersistenceWorker(database, run_cfg, bus, normalise=plate_normaliser_for(run_cfg))
        worker.start()
        print(f"persisting to {database.url}")

    stats = pipeline.run(
        max_frames=args.max_frames,
        show=args.show,
        save_video=args.save_video,
        save_frames_dir=args.save_frames,
        frame_stride=args.stride,
    )
    print("-" * 60)

    transitions = Counter(e.type for e in bus.recorded if e.type in (SLOT_OCCUPIED, SLOT_FREED))
    final = {sid: fsm.status.value for sid, fsm in pipeline.engine.fsms.items()}
    print(
        f"frames={stats['frames']}  steady_fps={stats['steady_fps']:.1f}  "
        f"mean_fps={stats['fps']:.1f} (includes {stats['warmup_seconds']:.1f}s model warmup)  "
        f"final={stats['summary']}"
    )
    print(f"transitions: occupied={transitions[SLOT_OCCUPIED]} freed={transitions[SLOT_FREED]}")
    for slot_id in sorted(final):
        states = [s.value for _, s in pipeline.engine.fsms[slot_id].history]
        print(f"  {slot_id:>4} {final[slot_id]:<16} history={'>'.join(_dedupe(states)) or 'FREE'}")

    if worker is not None:
        # The frame loop is finished, so nothing more will be published: `stop()`
        # writes the tail of the bus and reports what actually landed.
        persisted = worker.stop()
        print("-" * 60)
        # Handled rather than written: the per-frame SUMMARY events on this bus are
        # accepted by the writer and deliberately stored as no row at all.
        print(
            f"persisted to {database.url}\n"
            f"  {persisted['events_written']} of {len(bus.recorded)} events handled in "
            f"{persisted['batches']} transaction(s), bus dropped {persisted['dropped_by_bus']}"
        )
        with database.scope() as session:
            repos = Repositories(session, normalise=plate_normaliser_for(run_cfg))
            bays = repos.slots.for_camera(camera.id)
            intervals = [row for bay in bays for row in repos.occupancy.history(bay.id)]
            still_taken = sum(1 for row in intervals if row.ended_at is None)
            print(
                f"  rows: {len(bays)} bay(s), {len(intervals)} stay(s) "
                f"({still_taken} still occupied), {repos.events.total()} logged event(s)"
            )
            # A bay the engine never saw change is still UNKNOWN here: the row
            # records committed transitions, and a FREE bay that stayed FREE
            # produced none. That is the run's truth, not a missing write.
            print("  " + ", ".join(f"{bay.slot_code}={bay.current_status.value}" for bay in bays))
        print("  " + format_failures(worker.failures, total=worker.failure_total))
        if worker.failure_total:
            print("  PERSISTENCE INCOMPLETE - the rows above are not the whole run.")
    return 0


def _dedupe(seq):
    out = []
    for item in seq:
        if not out or out[-1] != item:
            out.append(item)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
