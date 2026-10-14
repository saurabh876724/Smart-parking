"""Run the gate pipeline over one camera source (Phase 6).

    python scripts/run_gate.py --fixture          # the labelled acceptance run
    python scripts/run_gate.py --camera gate_cam1 # real footage, YOLO + ByteTrack
    python scripts/run_gate.py --tracks boxes.json --source clip.mp4 --no-anpr
    python scripts/run_gate.py --persist          # write the sessions to SQLite

A gate run is: vehicle tracks in, plate reads and line crossings out, and the two
joined into sessions with durations. This script prints each session event as it
happens - the doc's `ENTER plate X at t1 ... EXIT at t2, duration` - then, when the
input carries ground truth, scores every observed entry, exit and duration against
the frame it was supposed to happen on.

`--tracks` replays a labelled box file instead of running YOLO, for the same reason
Phase 5 has one: a rendered clip has no real cars in it, so the vehicle detector
finds nothing, and a gate stage with no tracks has nothing to cross a line. When it
is used every line of output says SYNTHETIC, because what it proves is the software
chain, not a barrier.

`--persist` is Phase 7's addition and the only place a gate run touches a database:
the same events printed above are handed to the persistence worker, which writes
them on its own thread so inference never waits for SQLite. The database path comes
from `configs/app.yaml` (`--db` overrides it for a scratch file), and the run ends
with what actually landed plus any event the database refused - a session table
with no rows behind it is a bug, not a small run.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from sqlalchemy import inspect  # noqa: E402

from smart_parking.anpr.engine import build_ocr  # noqa: E402
from smart_parking.core.config import (  # noqa: E402
    CameraConfig,
    DatabaseConfig,
    gate_config_for,
    load_config,
)
from smart_parking.core.events import RecordingEventBus  # noqa: E402
from smart_parking.core.schemas import (  # noqa: E402
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    VIOLATION,
    Event,
    SessionStatus,
    Track,
)
from smart_parking.db.repositories import Repositories  # noqa: E402
from smart_parking.db.session import Database  # noqa: E402
from smart_parking.pipeline.gate_pipeline import GatePipeline  # noqa: E402
from smart_parking.services.persistence import (  # noqa: E402
    PersistenceWorker,
    format_failures,
    plate_normaliser_for,
)
from smart_parking.tracking.replay import load_track_file  # noqa: E402

FIXTURE_TRACKS = "data/fixtures/gate_visit_tracks.json"


def load_replay(path: str, cfg) -> tuple[dict[int, list[Track]], list[dict]]:
    """Labelled boxes grouped by frame, plus the fixture's ground truth."""
    return load_track_file(path, cfg)


def describe(event: Event, t0: float) -> str:
    """One line per session event, in seconds from the start of the clip.

    The pipeline's clock is video time on a clip and wall time on a source with no
    frame rate; either way `event.timestamp - t0` is the elapsed time the operator
    cares about.
    """
    p = event.payload
    when = event.timestamp - t0
    track = f"track {p.get('track_id', '-'):>3}"
    plate = p.get("plate") or "no plate"
    if event.type == VEHICLE_ENTERED:
        return f"  ENTER   t={when:6.2f}s  {track}  plate {plate:<12} session {p['session_id']}"
    if event.type == VEHICLE_EXITED:
        duration = p.get("duration")
        reason = "by " + (p.get("exit_reason") or "line")
        tail = f"duration {duration:.2f}s" if duration is not None else "no duration"
        return f"  EXIT    t={when:6.2f}s  {track}  plate {plate:<12} {reason:<8} {tail}"
    if event.type == VIOLATION:
        return (
            f"  ANOMALY t={when:6.2f}s  {track}  plate {plate:<12} "
            f"{p.get('kind')}  session {p['session_id']}"
        )
    return f"  {event.type} t={when:6.2f}s {p}"


def score(
    expectations: list[dict],
    sessions_by_track: dict,
    fps: float,
    t0: float,
    tol: float,
    plates_expected: bool = True,
):
    """Compare the run against the frame each event was supposed to land on.

    ``plates_expected`` is False on a `--no-anpr` run: the crossings are still
    scored, but a session with no plate is that run's design, not a defect.
    """
    rows = []
    ok = 0
    for want in expectations:
        track_id = int(want["track_id"])
        found = sessions_by_track.get(track_id, [])
        session = found[0] if found else None
        problems: list[str] = []

        def frame_of(ts):
            # The pipeline stamps frame f at t0 + (f+1)/fps, so recovering the
            # frame index is exact rather than "close enough".
            return None if ts is None else (ts - t0) * fps - 1.0

        def close(a, b):
            return a is not None and b is not None and abs(a - b) <= tol

        entry_frame = frame_of(session.entry_at) if session else None
        exit_frame = frame_of(session.exit_at) if session else None
        want_entry = want.get("expected_entry_frame")
        want_exit = want.get("expected_exit_frame")
        want_dwell = want.get("expected_dwell_frame")

        if want_entry is not None:
            if session is None:
                problems.append("no session at all")
            elif not close(entry_frame, want_entry):
                problems.append(f"entry at {entry_frame:.1f}, expected {want_entry}")
        if want_dwell is not None:
            if session is None:
                problems.append("no session for the dwell exit")
            elif session.exit_reason != "dwell":
                problems.append(f"exit reason {session.exit_reason!r}, expected dwell")
            elif not close(exit_frame, want_dwell):
                problems.append(f"dwell exit at {exit_frame:.1f}, expected {want_dwell}")
        elif want_exit is not None:
            if session is None:
                problems.append("no session for the exit")
            elif session.anomaly_reason == "exit_without_entry":
                # Correct for the exit-with-no-entry case: the anomaly record is
                # the answer, and it must carry no duration.
                if session.entry_at is not None or session.duration_s is not None:
                    problems.append("unmatched exit got a duration")
            elif not close(exit_frame, want_exit):
                problems.append(f"exit at {exit_frame:.1f}, expected {want_exit}")
        # A vehicle the fixture expects to produce nothing: any session at all is
        # the mismatch, and there is no plate for it to have got wrong.
        silent = want_entry is None and want_exit is None and want_dwell is None
        if silent and session is not None:
            problems.append(f"unexpected session {session.session_id}")

        if want_entry is not None and want_exit is not None and session is not None:
            want_duration = (want_exit - want_entry) / fps
            if session.duration_s is None:
                problems.append("no duration")
            elif abs(session.duration_s - want_duration) > max(tol / fps, 0.5):
                problems.append(
                    f"duration {session.duration_s:.2f}s, expected {want_duration:.2f}s"
                )

        truth = want.get("ground_truth")
        plate_note = "-"
        if truth and not silent:
            if session is None:
                plate_note = "no session"
            elif session.plate is None:
                plate_note = "not read"
                if plates_expected:
                    problems.append("no plate bound")
            elif session.plate == truth:
                plate_note = "correct"
            else:
                plate_note = f"WRONG {session.plate}"
                problems.append("wrong plate")

        if want_entry is None and want_exit is None and want_dwell is None and session is not None:
            problems.append("produced an event for a silent vehicle")
        verdict = "OK" if not problems else "MISMATCH: " + "; ".join(problems)
        if not problems:
            ok += 1
        rows.append(
            {
                "track": track_id,
                "case": want.get("case", ""),
                "entry": f"{entry_frame:.0f}" if entry_frame is not None else "-",
                "want_entry": want_entry,
                "exit": f"{exit_frame:.0f}" if exit_frame is not None else "-",
                "want_exit": want_dwell if want_dwell is not None else want_exit,
                "reason": session.exit_reason if session else "-",
                "duration": f"{session.duration_s:.2f}" if session and session.duration_s else "-",
                "status": session.status.value if session else "none",
                "plate": plate_note,
                "verdict": verdict,
            }
        )
    return rows, ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", default="gate_cam1", help="id from configs/cameras.yaml")
    ap.add_argument("--source", default=None, help="override the camera source")
    ap.add_argument("--tracks", default=None, help="labelled box file: replay instead of YOLO")
    ap.add_argument(
        "--fixture",
        action="store_true",
        help="the Phase 6 acceptance run: camera gate_visit, its labelled boxes, "
        "and a dwell timer short enough to fire inside a 6 s clip",
    )
    ap.add_argument("--stride", type=int, default=None, help="run inference every Nth frame")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--dwell-exit-s", type=float, default=None, help="override the dwell timer")
    ap.add_argument(
        "--tolerance",
        type=float,
        default=2.0,
        help="frames an observed event may be off by (default 2)",
    )
    ap.add_argument("--no-anpr", action="store_true", help="crossings only, skip plate reading")
    ap.add_argument(
        "--persist",
        action="store_true",
        help="write the sessions this run produces to SQLite (Phase 7)",
    )
    ap.add_argument(
        "--db",
        default=None,
        help="database URL for --persist instead of database.url in configs/app.yaml",
    )
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--save-video", default=None)
    ap.add_argument("--quiet", action="store_true", help="no per-event lines")
    args = ap.parse_args()

    cfg = load_config(root=ROOT)
    if args.fixture:
        args.camera = "gate_visit"
        args.tracks = args.tracks or FIXTURE_TRACKS
        # The clip is 6 s long; a timer tuned for a real barrier (12 s) could
        # never fire inside it, so the acceptance run shortens it. Everything it
        # proves is still about the software, not about a gate's timing.
        args.dwell_exit_s = args.dwell_exit_s or 2.0
    camera = next((c for c in cfg.cameras if c.id == args.camera), None)
    if camera is None:
        camera = CameraConfig(id=args.camera, name=args.camera, source=args.source or "")
    if args.source:
        camera.source = args.source
    source = camera.source if Path(camera.source).is_absolute() else str(ROOT / camera.source)
    if not camera.source or not Path(source).exists():
        print(f"{source}: no such file - set a source in configs/cameras.yaml or pass --source")
        return 2

    gate = gate_config_for(cfg, camera)
    if args.dwell_exit_s is not None:
        gate = gate.model_copy(update={"dwell_exit_s": args.dwell_exit_s})
    if not gate.enabled or not gate.lines:
        print(
            f"camera {camera.id} has no gate lines - give it a gate_file in "
            f"configs/cameras.yaml (see configs/gates/)"
        )
        return 2

    replay, expectations = (None, [])
    if args.tracks:
        replay, expectations = load_replay(args.tracks, cfg)

    # The database needs this run's camera to exist before its first session does,
    # and a `--camera` id that is not in cameras.yaml is still a camera as far as
    # the foreign key is concerned.
    run_cfg = cfg
    if args.persist and not any(c.id == camera.id for c in cfg.cameras):
        run_cfg = cfg.model_copy(update={"cameras": [*cfg.cameras, camera]})

    bus = RecordingEventBus()
    ocr = None if (args.no_anpr or not cfg.anpr.enabled) else build_ocr(cfg.anpr)
    pipeline = GatePipeline(
        cfg,
        camera,
        gate=gate,
        bus=bus,
        anpr=ocr is not None,
        ocr=ocr,
        replay_tracks=(lambda frame: replay.get(frame, [])) if replay is not None else None,
    )

    lines = ", ".join(f"{l.name}={l.role}/{l.direction}" for l in gate.lines)
    print(
        f"camera={camera.id}  source={source}\n"
        f"tracks={'labelled replay from ' + str(args.tracks) if replay is not None else 'YOLO + ByteTrack'}"
        f"  plates={'off (--no-anpr)' if ocr is None else cfg.anpr.ocr.engine}\n"
        f"gate lines: {lines}  zone={'yes' if gate.zone else 'no'}  "
        f"dwell exit={gate.dwell_exit_s:.1f}s  track timeout={gate.track_timeout_s:.1f}s\n"
        + "-" * 78
    )
    if replay is not None:
        print("SYNTHETIC REPLAY: vehicle boxes were supplied, not detected. "
              "Sessions below prove the chain, not a gate.")

    worker = None
    if args.persist:
        database = Database.from_config(
            run_cfg, DatabaseConfig(url=args.db) if args.db else run_cfg.database
        )
        if not inspect(database.engine).has_table("parking_sessions"):
            # The schema is the migrations' job, not a run's: starting inference
            # against an uncreated database would fail every write one event at a
            # time and fill the report with errors that mean "you have not migrated".
            print(
                f"{database.url} has no tables - run "
                f"`python scripts/migrate.py upgrade head` first"
            )
            return 2
        worker = PersistenceWorker(database, run_cfg, bus, normalise=plate_normaliser_for(run_cfg))
        # Starts on the seeding: a missing calibration file is reported here,
        # before inference, rather than from a thread nobody is watching.
        worker.start()
        print(f"persisting to {database.url}")

    t0_holder: dict[str, float] = {"t0": 0.0, "fps": 0.0}

    def emit(event: Event) -> None:
        line = describe(event, t0_holder["t0"])
        if not args.quiet:
            print(line)

    pipeline.on_event = emit
    # The clip's own rate is what the gate times itself in; the pipeline reads it
    # the same way, and this copy is only for printing the event lines and the
    # frame columns.
    cap = cv2.VideoCapture(source)
    t0_holder["fps"] = cap.get(cv2.CAP_PROP_FPS) or 0.0
    cap.release()
    # Set immediately before run(): the pipeline's clock origin is the moment it
    # enters run(), so any slack here shows up as an offset in every frame number
    # the report prints.
    t0_holder["t0"] = time.time()
    summary = pipeline.run(
        max_frames=args.max_frames,
        show=args.show,
        save_video=args.save_video,
        frame_stride=args.stride,
    )
    # With no frame rate on the source the pipeline falls back to wall clock, so
    # the event lines above are elapsed real seconds rather than clip seconds.
    if t0_holder["fps"] <= 0:
        print("(no fps on the source: the t= values above are wall-clock)")

    print("-" * 78)
    repeated = sum(pipeline.logic.duplicates.values())
    print(
        f"frames {summary['frames']}  crossings {summary['crossings']}  "
        f"repeat crossings ignored {repeated}  "
        f"duplicate entries refused {summary['duplicate_crossings']}  "
        f"sessions {summary['sessions']} ({summary['open_sessions']} still open)"
    )
    print(
        f"throughput   {summary['fps']:.1f} frames/s overall "
        f"(includes {summary['warmup_seconds']:.1f}s model warm-up), "
        f"{summary['steady_fps']:.1f} frames/s steady state"
    )

    by_track: dict[int, list] = defaultdict(list)
    for session in pipeline.sessions.sessions.values():
        by_track[session.track_id].append(session)

    fps, t0 = t0_holder["fps"], t0_holder["t0"]

    def frame_col(ts) -> str:
        if ts is None or fps <= 0:
            return "-"
        return f"{(ts - t0) * fps - 1.0:.0f}"

    print("\nsession table")
    print(f"{'id':<18}{'plate':<14}{'entry':>8}{'exit':>8}{'dur':>8}  {'reason':<7}{'status':<13}notes")
    for session in pipeline.sessions.sessions.values():
        notes = " / ".join(x for x in (session.review_reason, session.anomaly_reason) if x) or "-"
        duration = f"{session.duration_s:.2f}" if session.duration_s is not None else "-"
        print(
            f"{session.session_id:<18}{session.plate or '-':<14}"
            f"{frame_col(session.entry_at):>8}{frame_col(session.exit_at):>8}"
            f"{duration:>8}  {session.exit_reason or '-':<7}{session.status.value:<13}{notes}"
        )
    print("(entry/exit columns are source frame numbers when the clip carries an fps)")

    if expectations and fps > 0:
        rows, ok = score(expectations, by_track, fps, t0, args.tolerance, ocr is not None)
        print("\nground truth  (entry/exit are observed frames; want/dur are the fixture's)")
        if ocr is None:
            print("  plate reading was off, so only the crossings are scored")
        print(f"{'track':>5}  {'entry':>6}{'want':>6}  {'exit':>6}{'want':>6} {'dur':>7}  {'plate':<12}verdict")
        for r in rows:
            want_e = "-" if r["want_entry"] is None else str(r["want_entry"])
            want_x = "-" if r["want_exit"] is None else str(r["want_exit"])
            print(
                f"{r['track']:>5}  {r['entry']:>6}{want_e:>6}  "
                f"{r['exit']:>6}{want_x:>6} {r['duration']:>7}  "
                f"{r['plate']:<12}{r['verdict']}"
            )
        print(f"\n{ok} of {len(rows)} fixture vehicles behaved exactly as designed")
        if ok != len(rows):
            print("MISMATCH - Phase 6 acceptance is not met by this run.")
        else:
            print("SYNTHETIC FIXTURE: acceptance of the software chain, not a field result.")

    anomalies = [s for s in pipeline.sessions.sessions.values() if s.status is SessionStatus.ANOMALY]
    print(
        f"\n{len([e for e in bus.recorded if e.type == VEHICLE_ENTERED])} entered, "
        f"{len([e for e in bus.recorded if e.type == VEHICLE_EXITED])} exited, "
        f"{len([e for e in bus.recorded if e.type == VIOLATION])} violations, "
        f"{len(anomalies)} anomaly sessions, events on the bus: {len(bus.recorded)}"
    )

    if worker is not None:
        # Inference is finished, so nothing more will be published: `stop()` writes
        # the tail of the bus and returns what the run actually achieved.
        persisted = worker.stop()
        print("\npersisted to " + database.url)
        # "handled", not "written": a per-frame SUMMARY is accepted by the writer
        # and deliberately stored as no row at all, which is the next line's job.
        print(
            f"  {persisted['events_written']} of {len(bus.recorded)} events handled in "
            f"{persisted['batches']} transaction(s), bus dropped {persisted['dropped_by_bus']}"
        )
        with database.scope() as session:
            repos = Repositories(session, normalise=plate_normaliser_for(run_cfg))
            print(
                f"  rows: {len(repos.sessions.all())} session(s), "
                f"{len(repos.plates.all())} plate(s), {len(repos.vehicles.all())} vehicle(s), "
                f"{len(repos.slots.for_camera(camera.id))} bay(s), "
                f"{repos.events.total()} logged event(s)"
            )
        print("  " + format_failures(worker.failures, total=worker.failure_total))
        if worker.failure_total:
            print("  PERSISTENCE INCOMPLETE - the rows above are not the whole run.")
        if replay is not None:
            print("  SYNTHETIC REPLAY: these rows show the chain working, not a field record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
