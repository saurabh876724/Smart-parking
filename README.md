# AI-Based Smart Parking Management System

Perception + occupancy pipeline built to the architecture in
`AI_Smart_Parking_Architecture.pdf` (YOLO · ByteTrack · OpenCV · ANPR · FastAPI ·
PostgreSQL · React), developed locally on an RTX 4050 with all processing on-device.

**Status: Phases 1-4 complete and verified on real footage.** Phase 5 (ANPR) is
built and measured, and it is *blocked in the field*: no recording on this
machine has plates wide enough for the OCR stage, so end-to-end accuracy is
unproven on real footage - see the Phase 5 limitations. Phase 6 (gate lines,
entry/exit sessions, track-to-plate binding) is built and its correctness is
proven frame-for-frame against a labelled synthetic gate clip; like Phase 5 it
has no real gate recording to validate against. Phase 7 (SQLAlchemy + Alembic
persistence on SQLite) is built, migrations verified, and every visit the pipeline
prints is now readable back out of a database - with the same field caveat
inherited from Phases 5 and 6. Phase 8 (FastAPI: JWT auth with four roles, the
doc's REST surface, a WebSocket hub and an MJPEG stream over that same event bus)
is built, tested and served on this machine; it reads real plates off no real
gate, for the same reason as above. Phase 9 (React + TypeScript + Vite dashboard:
login, live slot map, session table, revenue chart, WebSocket-driven updates) is
built and running locally at http://localhost:5173 against the Phase 8 API. Phase 10
(deployment: Docker Compose with PostgreSQL, GPU-enabled FastAPI, nginx reverse proxy,
HTTPS, backup/restore scripts, 24-hour soak test) is scaffolded with full deployment
documentation in `deploy/README.md`. Docker is not installed on this development machine,
so the soak test has not been run - see limitations below.

```
camera -> frame reader (grabber if live, inline if replay) -> YOLO vehicles
       -> ByteTrack ids -> velocity gate (is it slow enough to be parking?)
       -> footprint containment -> slot assignment (one slot per vehicle)
       -> per-slot debounce FSM -> events

       gate camera, in parallel and independent of all of the above:
       vehicle track -> plate detector -> plate crop -> preprocessing
       -> OCR -> plate validation -> multi-frame voting -> stable plate -> track id
       vehicle track -> gate line crossing (entry / exit / zone dwell)
       -> + plate -> parking session (ENTER t1, EXIT t2, duration)

       then, as just another subscriber to the same event bus (Phase 7):
       events -> persistence worker (its own thread) -> repositories
       -> SQLAlchemy -> Alembic-managed SQLite schema

       and, as the same bus's other two subscribers (Phase 8):
       serve.py (lifespan) -> CameraRunner per camera (its own thread, paced to
                               the clip's own frame rate)
         -> FrameHub  (latest frame per camera)   -> GET .../snapshot, .../stream.mjpg
         -> WebSocketHub (per-viewer queue)       -> WS /ws/live, /ws/cameras/{id}/overlay
       browser -> POST /auth/login (Argon2id) -> JWT -> every other route,
                role-checked; plate text masked unless the role may reveal it
```

## Documentation

| Document | What it is |
|---|---|
| [`docs/Smart_Parking_System_Technical_Report.pdf`](docs/Smart_Parking_System_Technical_Report.pdf) | 10-page technical report: overview, feature/status tables, architecture, AI pipeline, API + security, frontend, verification evidence, risks |
| [`docs/project_report.md`](docs/project_report.md) | Verified audit of the running system: status verdict, feature table with file:line evidence, test results, defect log |
| [`docs/architecture.md`](docs/architecture.md) | System architecture diagram (Mermaid) + component walkthrough |
| [`docs/working_flow.md`](docs/working_flow.md) | Working-flow diagram (Mermaid) + decision branches |
| [`docs/viva_and_demo.md`](docs/viva_and_demo.md) | Viva explanation, 5-minute demo script, Q&A, limitations |
| [`docs/linkedin_post.md`](docs/linkedin_post.md) | Project write-up for LinkedIn (short post + full article) |

## Quick start

Environment: this project runs on the system Python 3.10 that already has
`torch 2.11+cu128` and `ultralytics 8.4`. Nothing is reinstalled.

On this machine `py -3.10`, plain `python` and the full path
`C:\Users\Saurabh\AppData\Local\Programs\Python\Python310\python.exe` all
resolve to the same interpreter (verified 2026-10-12 in Git Bash *and*
PowerShell), so any of the three works in the commands below. If a terminal
ever reports that `py` is not recognised - a shell whose PATH predates the
launcher install - use the full path or plain `python`; do not reinstall
anything. (`py --list-paths` shows what the launcher can see.)

```bash
cd "C:\Users\Saurabh\Downloads\Parking ai sysytem"
py -3.10 -m pytest tests -q                      # 542 tests (309 from Phases 1-7 + 233 in Phase 8)
py -3.10 scripts/run_detect.py    --show         # Phase 1
py -3.10 scripts/calibrate.py     --grid         # Phase 2 (see below for mouse mode)
py -3.10 scripts/run_occupancy.py --show         # Phase 3
py -3.10 scripts/measure_velocity.py             # Phase 4: pick a threshold for a camera
py -3.10 scripts/measure_plates.py --camera gate_cam1   # Phase 5: is this footage usable at all?
py -3.10 scripts/bench_anpr.py                   # Phase 5: characterise OCR on the rendered fixture
py -3.10 scripts/run_anpr.py --camera gate_cam1  # Phase 5: run the plate pipeline
py -3.10 scripts/calibrate.py     --gate         # Phase 6: draw gate lines + exit zone
py -3.10 scripts/run_gate.py      --fixture      # Phase 6: the labelled acceptance run
py -3.10 scripts/run_gate.py      --camera gate_cam1   # Phase 6: real footage, YOLO + ByteTrack
py -3.10 scripts/migrate.py       upgrade head        # Phase 7: build the schema Alembic owns
py -3.10 scripts/run_gate.py      --fixture --persist # Phase 7: the same run, written to SQLite
py -3.10 scripts/run_occupancy.py --persist           # Phase 7: bays and stays to SQLite
py -3.10 scripts/dev_secret.py                         # Phase 8: create the signing key (data/.secret)
py -3.10 scripts/create_user.py --email root@lot.example --role ADMIN   # Phase 8: the first account
py -3.10 scripts/serve.py       --persist              # Phase 8: http://127.0.0.1:8000/docs + the cameras
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--source <file\|0\|rtsp://...>` | any video file, webcam index or RTSP url |
| `--show` | open the operator window (also `--save-frames DIR`, `--save-video FILE`) |
| `--stride 3` | infer every 3rd frame - the main GPU saving on a lot camera |
| `--no-track` | plain per-frame detections; identity gating is switched off (see below) |
| `--loop` | replay a file forever, for soak testing |
| `--gate` (`calibrate.py`) | draw entry/exit lines and the exit zone instead of slot polygons |
| `--fixture` (`run_gate.py`) | the labelled Phase 6 acceptance run: synthetic clip + known event frames |
| `--tracks FILE` (`run_gate.py`, `run_anpr.py`) | replay a labelled box file instead of running YOLO |
| `--no-anpr` (`run_gate.py`) | crossings and sessions only, no plate stage |
| `--persist` (`run_gate.py`, `run_occupancy.py`) | Phase 7: write the run's events to SQLite on a worker thread |
| `--db URL` (both, with `--persist`) | a database other than `database.url` in `configs/app.yaml` |
| `--url URL` (`migrate.py`, `serve.py`, `create_user.py`) | a database other than `database.url` in `configs/app.yaml`; also `SMART_PARKING_DATABASE_URL` |
| `--role ADMIN\|OPERATOR\|VIEWER\|CUSTOMER` (`create_user.py`) | Phase 8: uppercase, argparse-enforced |
| `--password VALUE` (`create_user.py`) | Phase 8: omit it to be asked twice instead; a value lands in shell history |
| `--list` (`create_user.py`) | Phase 8: show the accounts in a database, deactivated ones included |
| `--persist` (`serve.py`) | Phase 8: also run the Phase 7 worker, so the served cameras write their visits |
| `--no-pipelines` (`serve.py`) | Phase 8: serve the API with no camera threads (no GPU work, no frames to stream) |
| `--host/--port` (`serve.py`) | defaults to `127.0.0.1:8000` - nothing listens on the LAN unless told to |
| `--check` / `--rotate --force` (`dev_secret.py`) | Phase 8: report the key this machine would use / replace it |

## Layout

Layered by responsibility. The rule that keeps it testable: `parking/`,
`detection/`, `tracking/` and `video/` never import `db/` or `api/` - only
`services/` will talk to both worlds. `services/session_service.py` (Phase 6) is
pure in-memory state and a guard test asserts it; `services/persistence.py`
(Phase 7) is the one module allowed to know both halves, and
`test_the_ai_layers_still_import_nothing_from_the_database` fails if that seam
ever moves into a detector. Phase 8 keeps three more seams, each one a test that
reads source text rather than behaviour: `api/` never opens `configs/app.yaml`
(`test_the_api_layer_never_reads_the_configuration_file`), never holds a session
or commits one (`test_the_api_layer_never_opens_a_database_session` - a route
that needs a row asks a repository for it), and `db/` never learns that an API
exists (`test_the_database_layer_does_not_know_the_api_exists`).

```
configs/          app.yaml (all thresholds + database.url), cameras.yaml,
                  slots/*.json, gates/*.json
src/smart_parking/
  core/           schemas.py, events.py (EventBus), config.py, logging.py
  video/          sources.py (file/webcam/RTSP), grabber.py (threaded, live only)
  detection/      vehicle_detector.py (YOLO wrapper, FP16, class filter)
  tracking/       tracker.py (ByteTrack behind a flag), velocity.py (Phase 4 gate),
                  replay.py (labelled box file -> tracks, no detector)
  parking/        geometry.py, slot_fsm.py, occupancy_engine.py, gate_logic.py,
                  calibration.py (slots + --gate lines), render.py
  anpr/           engine.py, plate_detector.py, preprocess.py, reader.py,
                  validation.py, voting.py, association.py, pipeline.py
  services/       session_service.py (Phase 6: crossings + plates -> visits),
                  persistence.py (Phase 7: events -> rows, on a worker thread)
  db/             base.py (Base, naming convention, naive-UTC helper),
                  models.py (the nine tables), session.py (engine + pragmas),
                  repositories/ (camera, slot, plate, vehicle, session, event,
                  user and audit - Phase 8's two),
                  migrations/ (alembic env + 0001 + 0002)
  pipeline/       lot_pipeline.py, gate_pipeline.py
  api/            Phase 8. app.py (create_app + lifespan: the only place the
                  camera threads and the worker are started), security.py
                  (Argon2id, JWT issue/decode, mask_plate), deps.py (bearer ->
                  CurrentUser, require_roles), schemas.py (the DTOs; no response
                  model has a password_hash field), live.py (FrameHub,
                  WebSocketHub, CameraRunner, CameraManager, QuietBus),
                  routers/ (auth+users, cameras, slots, sessions, plates, stats,
                  audit, health, live_ws)
scripts/          run_detect.py, calibrate.py, run_occupancy.py, measure_velocity.py,
                  run_anpr.py, bench_anpr.py, measure_plates.py, probe_ocr_cores.py,
                  make_plate_fixture.py, make_gate_fixture.py, run_gate.py,
                  migrate.py (Phase 7: current/heads/history/upgrade/downgrade/
                  check/revision, never called from a pipeline run),
                  serve.py (Phase 8: uvicorn over the configured cameras),
                  create_user.py (Phase 8: the first account, hashes and appends
                  a user_created audit row, refuses a duplicate email),
                  dev_secret.py (Phase 8: writes the signing key to data/.secret)
alembic.ini       Phase 7: the migration context, URL resolved by env.py
tests/unit/       geometry, slot FSM, occupancy engine, velocity gate,
                  plate validation, plate voting, plate association,
                  ANPR pipeline, ANPR config,
                  gate config, gate line logic, gate sessions,
                  gate pipeline, gate calibration,
                  db schema (migrations, keys, constraints, indexes),
                  db repositories (units of work, idempotency),
                  persistence (Phase 7 end-to-end: event -> row -> read back),
                  api config (ApiConfig + the shipped yaml),
                  api auth (passwords, tokens, refresh, expiry, role claims),
                  api endpoints (every route, every role, the error envelope),
                  api live (FrameHub/WebSocketHub/CameraRunner, pacing, MJPEG)
```

## Design decisions worth knowing

**Slots are stored normalised (0-1)** so one calibration survives a resolution
change; `OccupancyEngine` converts to pixels once per frame size and caches.
`test_same_calibration_works_at_two_resolutions` pins that.

**Assignment uses containment, not the doc's overlap ratio.** The doc scores a
slot as `area(footprint ∩ slot) / area(slot)`. Measured on the real clip, a
small car parked perfectly inside a far-row bay scored **0.19** - below the
threshold - purely because the bay is bigger than the car. So:

- `containment_score` (footprint-denominated) decides *which slot a vehicle is in*
- `overlap_score` (slot-denominated) is kept for the straddle/double-park rule,
  where "how full is this bay" is the right question

**Asymmetric debounce timers.** `occupied_after_s: 0.6` but `free_after_s: 8.0`.
A false FREE double-books a slot; a delayed FREE costs a few seconds of
availability. The 8s free timer is also the occlusion hold: a lost track simply
stops providing evidence.

**Identity gating is only on when identity is real.** A new track arriving in an
already-pending bay restarts the dwell timer, so two successive cars produce two
occupancy events (and two billable sessions). With ByteTrack off the id changes on
every frame, and that same rule reset the timer forever - `--no-track` reported an
empty lot. `SlotFSM.identity_stable` is therefore set from whether tracking is on;
with it off, the FSM debounces on continuous slot evidence alone. Both directions
are pinned by tests.

**Live and replay use different readers.** The threaded grabber keeps only the
newest frame, which is correct for a camera but drops most of a clip when
inference lags behind the reader - an uncapped replay once processed 2 of 148
frames. So `LotPipeline.run(replay=True)` reads file frames inline and drives the
FSM timers from *video* time (`frame / source_fps`) instead of wall-clock time,
otherwise a 3x-speed replay would look like cars leaving 3x faster. `fps_limit`
in `cameras.yaml` is a live-camera throttle and does not apply to replay.

**ANPR will not run on the lot camera.** Plates are only readable when roughly
100+ px wide, so plate detection is gated to the entrance camera in Phase 5.

**Gate geometry is a separate drawing from slot geometry.** The same camera can
see a lane and a bay, but nothing in `gate_logic.py` knows about slots and
nothing in `occupancy_engine.py` knows about lines: the two stages share the
normalised 0-1 coordinate space and a tracker, and are calibrated independently
(`scripts/calibrate.py` for bays, `--gate` for lines). Sessions live in memory and
no gate module opens the database: Phase 7 reads the bus those stages already
publish, from `services/persistence.py`, and nothing in `parking/` or `pipeline/`
holds a connection.

**A dead camera yields UNKNOWN, never FREE.** `mark_all()` is called when frames
stop arriving, and recovered slots must re-prove occupancy from scratch.

## Phase 4 - track velocity gating

**Why.** Containment answers "is this vehicle's footprint inside a bay?", which a
car *driving through* a bay also satisfies for as long as it takes to cross it.
On `lot_shot5.mp4` that is not hypothetical: the car that rolls into A1 is still
moving at **38-90 px/s** in the final frames (containment 0.88), and the clip ends
before it stops. Phase 3 held it there for over `occupied_after_s` and reported
A1 as OCCUPIED - a bay booked by a vehicle that never parked.

**What gating is.** An eligibility condition, not a replacement for anything.
A track moving faster than the threshold contributes *no evidence* that frame;
the bay then behaves exactly as if no vehicle were in it. Containment still
decides which bay, the FSM still decides when to commit.

```
YOLO -> ByteTrack -> track history -> velocity -> gate -> assignment -> SlotFSM -> FREE/OCCUPIED
```

| Aspect | Implementation |
| --- | --- |
| Position | midpoint of the footprint's **bottom edge** - where the tyres are. The box centre drifts with pitch, and in an angled view the roof belongs to a different part of the lot than the wheels |
| Time source | supplied by the caller: `frame_index / source_fps` on replay, `time.time()` live. The gate never reads a clock itself |
| Units | **pixels per second at source resolution** |
| Smoothing | least-squares straight-line fit of x(t) and y(t) over the window, speed = `hypot(slope_x, slope_y)`. A 20 px single-frame jitter blip on a parked car reads as ~20 px/s at window 5, not 200 |
| Window | `smoothing_window` **samples**, i.e. frames the track was actually seen in: 15 frames = 0.5 s at stride 1, but 1.5 s at `--stride 3` |
| Gap handling | a track unseen for longer than `max_sample_gap_s` starts a fresh window, so an occlusion or camera reconnect is never averaged into a displacement |
| Unknown speed | fewer than 2 samples -> treated as **eligible**. The dwell timer is what filters a one-frame detection, not the gate |

**Configuration.**

```
Config file:   configs/app.yaml  ->  velocity_gating:
Config loader: smart_parking/core/config.py :: load_config()   (pydantic v2)
Config object: AppConfig.velocity_gating : VelocityGatingConfig
Consumer:      LotPipeline.__init__ builds VelocityGate(cfg.velocity_gating)
               -> OccupancyEngine(gate=...) -> applied in OccupancyEngine.update()
```

The gate module never reads YAML and holds no defaults of its own. Path
resolution is relative to the project root via `load_config(root=...)`, not the
working directory. A missing `velocity_gating:` section keeps gating **on** with
the documented defaults; an invalid value (`max_parking_velocity: 0`,
`smoothing_window: 1`) raises a `ValidationError` at load time.

```yaml
velocity_gating:
  enabled: true
  max_parking_velocity: 30.0    # pixels/second at source resolution, per camera
  smoothing_window: 15          # frames the least-squares slope is fitted over
  max_sample_gap_s: 1.0         # longer gap than this restarts the window
```

**How 30 px/s was chosen** - measured, not guessed. `scripts/measure_velocity.py`
runs the real detector + tracker over a clip and reports each track's speed
percentiles next to its best containment. On `lot_shot5.mp4` (478x850 @ 30 fps,
window 15):

| track | frames | med px/s | p90 | max | best containment |
| --- | --- | --- | --- | --- | --- |
| 1 (drives into A1) | 148 | **75.0** | 102.4 | 112.1 | 0.88 |
| 2 | 148 | 1.3 | 2.5 | 17.4 | 0.00 (aisle) |
| 3 (parked, B2) | 148 | 9.2 | 29.1 | 60.9 | 1.00 |
| 4 (parked, A2) | 133 | 2.4 | 5.0 | 26.3 | 0.96 |
| 5 (parked, B1) | 89 | 3.8 | 56.1 | 74.5 | 1.00 |
| 6 (parked, B3) | 11 | 6.5 | 9.2 | 9.2 | 1.00 |

Stationary tracks sit at a 1.3-9.2 px/s median, the driving car at 75 px/s - an
8.2x gap, whose geometric midpoint is ~26 px/s. 30 px/s sits just above that with
margin. A 5-frame window was too noisy to separate them at all (stationary p90
spiked to 134 px/s, above the driving car's median); 15 frames halves the spikes.

**Pixel velocity is not vehicle speed.** 30 px/s here means 30 px/s *in this
478x850 frame from this camera at this mounting height*. The same car at 30 km/h
produces a completely different number under a wider lens, a higher mount, or a
1080p sensor, and even within one frame a car near the camera moves through more
pixels than the same car far away. The threshold is therefore **per camera** and
must be re-measured with `scripts/measure_velocity.py` whenever the view, the
resolution or the lens changes. Converting to km/h needs a homography from image
coordinates to ground coordinates - a calibration step that is not in scope here.

**Interaction with the other three mechanisms** - they stay separate:

- *SlotFSM*: unchanged. Gating removes evidence; the FSM's own asymmetric timers
  decide what absence means. A gated-out track in a bay that was OCCUPIED starts
  the 8 s `free_after_s` timer, which is the same path a genuinely departing car
  takes, so a brief occlusion or a jitter spike cannot free a bay.
- *Containment*: unchanged, and still footprint-denominated
  (`area(footprint ∩ slot) / area(footprint)`). Gating happens before it and does
  not touch `geometry.py`.
- *Straddle detection*: still slot-denominated overlap across two bays. It now
  only sees eligible tracks, so a car driving across two bays no longer raises a
  DOUBLE_PARK violation - that violation is meant for a *parked* vehicle.
- *`--no-track`*: no gate at all. Synthetic per-frame ids carry no motion
  information, and manufacturing velocity from them would be fiction, so the
  Phase 3 behaviour (and its false positive on A1) is preserved exactly.

**Test results.** 57/57 pass (30 pre-existing, 27 new). The new ones:

| Spec test | Where | What it proves |
| --- | --- | --- |
| A - fast aisle vehicle | `test_fast_vehicle_crossing_a_bay_never_claims_it` | a car crossing A1 at 200 px/s for 1.1 s never emits `slot_occupied` |
| A (control) | `test_the_same_crossing_without_gating_would_claim_the_bay` | the *same* motion with no gate **does** claim the bay, so the gate is what rejects it |
| B - parked vehicle | `test_vehicle_that_slows_and_stops_in_the_bay_occupies_it` | 800 px/s approach, brake inside A1, stay: exactly one `slot_occupied` |
| C - replay timing | `test_processing_speed_does_not_change_the_estimate`, `test_timestamp_epoch_is_irrelevant`, `test_frame_rate_changes_the_physical_answer` | identical (position, video-time) pairs fed at two wall-clock rates give identical speeds; the epoch is irrelevant; fps is what converts px/frame to px/s |
| D - configuration | `test_changing_the_configured_threshold_changes_gating`, `test_threshold_comes_from_the_yaml_file`, `test_missing_section_keeps_gating_enabled`, `test_invalid_threshold_is_rejected_at_load_time` | YAML -> `load_config` -> `VelocityGatingConfig` -> gate -> decision; 30 px/s rejects a 400 px/s car, 500 px/s admits it |
| E - no tracking | `test_no_track_mode_is_ungated_and_still_occupies`, `test_unstable_ids_do_not_reset_the_dwell_timer` | churning synthetic ids still occupy the bay |
| estimator maths | `test_constant_velocity_recovers_the_exact_slope`, `test_diagonal_motion_combines_both_axes`, `test_window_smooths_single_frame_jitter`, `test_a_long_gap_restarts_the_window` | slope exactness, axis combination, jitter rejection, gap restart |
| F - regression | full suite | no pre-existing test removed or weakened |

**Replay results** (`lot_shot5.mp4`, 148/148 frames, video time):

| | gating off (Phase 3) | gating on (Phase 4) |
| --- | --- | --- |
| Occupied | 4/5 | **3/5** |
| A1 | OCCUPIED by track 1 at 38-90 px/s | **FREE** - correctly withheld |
| A2 / B1 / B2 | OCCUPIED (containment 0.96-1.00) | OCCUPIED, unchanged |
| False bay claims | **1** | **0** |
| Spurious transitions | 0 | 0 |
| `--stride 3` | same as stride 1 | same as stride 1 (87 FPS processing, identical states) |

`lot_shot1.mp4` is unchanged at 4/5 either way - its only moving track (24.4 px/s
median) peaks at containment 0.39, below `occupy_score`, so it was never a
candidate.

The 4/5 that Phase 3 reported on `lot_shot5.mp4` was therefore **one bay too
many**, not a regression introduced by Phase 4: the trace in
`measure_velocity.py --trace 1` shows track 1 decelerating from 107 px/s to
37.7 px/s across the last five frames and the clip ending there. The car never
stops on camera, so no threshold could honestly call it parked. That case is
covered by unit test B instead.

**Performance.** Three back-to-back runs of the 148-frame replay: 26.7, 32.7,
29.4 FPS steady. The gate costs nothing measurable - it is a few dozen float ops
per track per frame against a YOLO forward pass. Across the whole session the
same command has reported 12-53 FPS depending on whether the RTX 4050 was idling
at 600 MHz or already warm, so treat any single number as noise.

**Known limitations (Phase 4).**

- Pixel velocity is not speed - see above. Per-camera threshold, no ground-plane
  homography yet.
- The window is counted in *samples*, so its time span changes with `--stride`
  and with the source frame rate. A lot camera at stride 3 gets a 1.5 s window,
  which adds up to ~1.5 s of lag before a newly stopped car is eligible, on top
  of the 0.6 s dwell.
- Smoothing lag cuts both ways: a car that stops needs roughly one window to be
  recognised as stopped. A very short stop-and-go in an aisle can dip under the
  threshold and be briefly eligible; the 0.6 s dwell is the second line of
  defence.
- Gating assumes the camera is static. With a moving or PTZ camera every track
  inherits the camera's motion, and the whole lot would read as moving.
- A track whose id switches (ByteTrack re-identification) inherits no history, so
  it restarts at "unknown -> eligible". Harmless for a parked car, and it is why
  the p90 column matters more than the median when choosing a threshold.

## Phase 5 - ANPR (automatic number plate recognition)

Runs on the **gate camera only**, next to the lot pipeline and independent of it.
It answers "which registration number belongs to this track" and nothing else:
no sessions, no billing, no database writes, and it never touches occupancy, the
FSM, slot containment or the velocity gate.

```
vehicle track (ByteTrack id)
  -> plate detector      text regions inside the bottom of the vehicle box
  -> plate crop          +6% margin: recognition clips the first/last character otherwise
  -> preprocess          grayscale (measured: the only switch that paid for itself)
  -> OCR                 PP-OCRv4 recognition, CPU
  -> validate            Indian plate shapes; homoglyph repair without changing length
  -> multi-frame voting  rolling window per track, 3 agreeing reads out of 7
  -> PlateResult         track_id, plate_text, confidences, bbox, stable
  -> PLATE_READ event    once per track per plate
```

Modules: `anpr/engine.py` (build the OCR engine from config), `plate_detector.py`,
`preprocess.py`, `reader.py`, `validation.py`, `voting.py`, `association.py`,
`pipeline.py`. Every one of them receives a Pydantic sub-config; none of them
opens a YAML file (there is a test for that, `test_anpr_config.py`).

### The camera decides whether ANPR is possible at all

Plate recognition is a pixel-budget problem before it is a software problem, so
`scripts/measure_plates.py` exists to be run *on a camera* before anyone promises
plate reads from it. It measures the widest plate-shaped region per vehicle and
compares it with `anpr.detector.min_plate_width`.

On the only gate-style clip available here (`data/samples/gate_traffic.mp4`,
480x360, 30 fps, 200 sampled frames, 748 vehicle detections):

| Measured on real footage | Value |
| --- | --- |
| widest plate region per vehicle - **minimum** | **10 px** |
| widest plate region per vehicle - **median** | **46 px** |
| widest plate region per vehicle - **maximum** | **86 px** |
| vehicles producing any text region at all | 29 / 748 (4%) |
| of those, regions passing the configured screens | 0 / 29 (0%) |
| OCR attempts made | 0 - nothing reached the 110 px floor |

**This footage cannot validate ANPR, and the pipeline confirms it rather than
faking it:** `scripts/run_anpr.py --camera gate_cam1` reports 138 vehicles,
0 plate candidates, 0 reads, 0 stable plates, 0 events.

So: **end-to-end OCR accuracy on a real entrance gate is unmeasured.** It needs a
recording from a camera pointed at the plate, at the resolution that gets the
plate above ~110 px in the frame. The software chain is instead validated on a
documented rendered fixture (next section), and that distinction is kept visible
everywhere: every fixture output is labelled `SYNTHETIC FIXTURE` or `Synthetic
fixture: this validates the software chain, not a gate`.

For scale, the readable band is 110 px (floor) to ~150 px (where per-frame exact
reads peak) - the clip's best plate is 86 px. Same framing at 1080p would
multiply those widths by 4, which is a projection from resolution arithmetic,
not a measurement.

### Fixture: what the software can be shown to do

`scripts/make_plate_fixture.py` renders 280 plates (clean, tilt, blur, noise,
JPEG-compressed, at plate widths from 30 to 200 px) with a ground-truth manifest.
`scripts/bench_anpr.py` measures against it. Every default in `configs/app.yaml`
came from these tables; nothing here is field accuracy.

**1) Recognition needs plate pixels** (exact plate box, so the detector is out of
the picture):

| plate width | exact reads | character accuracy |
| --- | --- | --- |
| 30 px | 2% | 2% |
| 45 px | 5% | 9% |
| 60 px | 20% | 31% |
| 80 px | 30% | 42% |
| 110 px | 38% | 53% |
| 150 px | **55%** | 62% |
| 200 px | 48% | 62% |

and by degradation, at all widths together: noise 41%, clean 32%, compressed 32%,
blur 20%, tilt 16% exact. Tilt and blur hurt most - which is the argument for a
camera that sees the plate square-on and a shutter fast enough to stop motion.

One frame is never the answer: a 55% per-frame read becomes a reliable plate only
after voting, which is why `minimum_observations` is 3.

**2) The plate detector.** Proposed regions inside the vehicle box, screens
relaxed so a miss can be told apart from a screen that is too tight:

- plate region located (IoU >= 0.3 with the true plate): **194/280 = 69%**
- of those, the production screens keep: 60/194 = 31%
- region widths: min 33 / median 100 / max 187 px, 0.8 regions per vehicle
- latency: **median ~30 ms per vehicle** pinned to the P-cores (155.8 ms in the
  same fixture run unpinned - see the core note below)

Detector recall, not the width floor, is the binding constraint here: the screens
throw away two thirds of the regions the text detector did find.

`det_limit_type` decides how DBNet scales the vehicle crop, and it is the one
knob where the obvious choice is wrong - re-measure it with
`python scripts/bench_anpr.py --limit-type min|max`:

| scaling | plate located | screens kept | median region width | ms/vehicle |
| --- | --- | --- | --- | --- |
| **max** (shipped) | 194/280 = 69% | 60 vehicles = 21% | 100 px | 29.6 |
| min | 215/280 = **77%** | 29 vehicles = 10% | 70 px | 115.1 |

`min` finds the plate more often but draws it *narrower*, so two thirds of its
findings fall under the width screen - and it pays 4x the time to do it, because
stretching the short side of a wide, low vehicle ROI up to its long side makes
the network run on aspect-ratio-squared pixels.

**3) Why `min_plate_width` is 110.** Recognition on the *detector's own* box,
bucketed by the width the screen actually sees:

| proposed width | exact reads |
| --- | --- |
| 125 px | 100% (n=24) |
| 150 px | 100% (n=16) |
| 175 px | 100% (n=20) |

Everything the screen let through at >= 110 px was read exactly; the losses in
table 1 happen below it. The floor is set where the pixels run out, not lower.

**4) Preprocessing was measured, not assumed** (`bench_anpr.py` section 4, each
row changes one switch against the raw control, 140 crops each):

| variant | exact | chars | ms/crop |
| --- | --- | --- | --- |
| raw (control) | 29% | 36% | 14.1 |
| **grayscale** | 29% | **39%** | 14.2 |
| + clahe | 24% | 34% | 14.4 |
| + upscale | 29% | 38% | 14.3 |
| upscale + clahe | 26% | 34% | 14.5 |
| + sharpen | 27% | 36% | 14.4 |
| + denoise | 29% | 38% | 28.7 |
| all on | 23% | 33% | 41.7 |

So the shipped defaults are `grayscale: true` and **everything else off**. CLAHE
actively hurts (-5% exact). Denoising doubles the cost for no gain. Running every
operation an ANPR tutorial mentions loses 6 points of accuracy and costs 3x the
time. Upscaling defaults to off (`min_crop_height: 0`): recognition pads its
input to a fixed 48x320 tensor anyway, so enlarging a thin crop buys pixels the
model resizes straight back down.

### OCR engine choice

One engine, `rapidocr-onnxruntime` 1.4.4 (PP-OCRv4 det + rec, ONNX, models
bundled in the wheel, `CPUExecutionProvider` only - `onnxruntime` here has no
CUDA provider, so plate reading is CPU work and does not compete with YOLO for
the GPU).

EasyOCR was rejected before being adopted, not after: installing it pulls
`opencv-python-headless` 5.0 over the working `opencv-python` 4.13 that Phases
1-4 depend on, and PaddleOCR would mean a second deep-learning runtime beside the
ONNX one already in the image. The spec asks for one engine with a concrete
reason not to add a second, and the concrete reason is that the second engine
breaks a dependency the finished phases need.

The plate detector drives the *stages* (`text_det.get_preprocess/infer/
postprocess_op`) rather than the engine's `__call__`, because the public call
throws away the per-box scores and filters boxes far too aggressively for
plates. `det_limit_type: max` is the measured choice in section 2 above: more
usable plates per vehicle, at a quarter of the time `min` needs.

### Validation: permissive about format, strict about structure

`anpr/validation.py` normalises (strip separators, uppercase, Devanagari and
fullwidth digits to ASCII, `INDIA`/`BHARAT` prefixes only at the very start or
end), then matches two shape families rather than one regex:

- `bharat_series`: `[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}(?:I|IND)?` - covers
  MH20AB1234, TS09D1234, DL12ABCD1234 and the international-mark suffixes
- `central_old`: `[0-9]{2}[A-Z]{1,2}[0-9]{4}` - pre-1987 style, e.g. 23AH1234

plus `extra_patterns` for anything a deployment needs to add without a code
change. `require_region_code` is **off** by default: the state-code list lags
behind new series, and rejecting a legitimate plate is worse than accepting an
unusual one.

Repair rules, which is where ANPR software usually starts inventing plates:

- homoglyphs are substituted **only where the pattern demands a digit**, so an
  `S` in a series letter is never "fixed"
- `max_repair_edits: 1` - one confused character is a typo, two is a garbage read
- `repair_lengths: [10]` - only full-length reads may be repaired. A 9-character
  string means the OCR *dropped* a character, and re-reading a letter as a digit
  to make the length work produces a plausible plate that belongs to nobody
- `repair_letter_positions: false` - the mirror case corrupts more plates than it
  rescues
- length is never changed, and characters are never inserted: a read that cannot
  match a shape is rejected with a reason and counts as a vote for *nothing*

### Multi-frame voting is its own component

`anpr/voting.py` knows nothing about OCR, OpenCV or tracks. It is a rolling
window of `(plate, score, timestamp)` ballots, and the spec's own example is a
test: `MH20AB1234` x4 against `MH20AB1284` x1 answers `MH20AB1234`.

- stable = at least `minimum_observations` (3) agreeing reads **and** agreement
  >= `minimum_agreement` (0.6) of the window; one read is never stable even at
  100% agreement
- rejected reads never enter the window, so garbage cannot dilute a good plate
- ties break on mean OCR score, then recency
- `anpr/association.py` owns one voter per track id and expires a window after
  `max_track_age_s` (3.0 s), so a recycled ByteTrack id cannot inherit the
  previous vehicle's plate - and the pipeline drops its "already reported" mark
  with it, letting a genuinely new vehicle report again

`AnprPipeline.process()` emits `PlateResult` (`track_id, plate_text,
plate_confidence, ocr_confidence, timestamp, bbox, stable, observations,
agreement`) and publishes `PLATE_READ` **once per track per plate**, with the
same transition semantics as the slot FSM.

### End-to-end on the labelled fixture replay

`data/fixtures/gate_synth.mp4` + `data/fixtures/gate_synth_tracks.json` (one
vehicle, 40 frames at 15 fps, plate growing 70 -> 200 px, ground truth
`MH20AB1234`; the vehicle boxes are supplied because YOLO cannot see a drawn
car):

```
frames processed 40  vehicles seen 40  plate candidates 24  OCR reads 24
accepted reads   24 (100% of reads)  of which repaired 0
stable plates    1  events published 1
track 1  predicted MH20AB1234  ground truth MH20AB1234  stable at 1.20s  correct
```

16 of the 40 vehicles produced no candidate at all - that is the 110 px floor
doing its job while the car was still too far away. The vote confirmed at the
third agreeing read (t=1.20s) and published one event. Honest caveat: on this
clip every accepted read was already correct, so the replay proves the
association and publish-once path; the outvoting of a wrong read is proved by
`test_plate_voting.py`, not by this footage.

### Performance

Measured on the fixture replay (stride 1, plate stage only, YOLO not involved)
with the process pinned to the CPU's P-cores:

| Stage | Measured |
| --- | --- |
| engine warm-up (first call builds the ONNX sessions) | 0.1 s, excluded from every figure below |
| plate detector | median ~30 ms/vehicle (29.6-29.7 over two pinned runs), 0.8 regions proposed per vehicle |
| OCR recognition | ~14 ms/crop, and flat across plate widths - the input is padded to a fixed 48x320 tensor, so a big plate costs the same as a small one |
| end-to-end (decode + detect + read + vote) | detector 114.5 ms/vehicle, OCR 79.1 ms/read (means, long tail included) |
| throughput on one gate stream | **6.1 frames/s** CPU-only, ~4x slower unpinned |

**Do not read one number off this machine and call it the ANPR frame rate.**
These boxes are hybrid CPUs (P-cores + E-cores) and Windows migrates the ONNX
worker threads onto the E-cores after a couple of seconds of sustained load: the
*identical* inference measures ~11-14 ms pinned to CPUs 0-11 and ~150 ms pinned
to 12-15 (`scripts/probe_ocr_cores.py fff` vs `f000`). That is a scheduling
effect, not the model, and it is also why the earlier width-vs-latency table was
a mirage: recognition cost does not depend on plate width at all.

A gate camera does not need 25 fps of plate reads. A vehicle needs three
agreeing reads inside the voting window, which at the 6.1 frames/s measured above
is a verdict in well under a second of processing per car; `--stride 3` is the
knob if the box is also doing something else. The GPU is unchanged by any of
this - nothing in Phase 5 touches it.

### Tests

90 ANPR tests, all passing, on top of the 57 from Phases 1-4 - none of which was
edited, weakened or skipped to make the new code pass.

| file | n | covers |
| --- | --- | --- |
| `test_plate_validation.py` | 38 | normalisation, shapes, homoglyph repair, rejection reasons |
| `test_plate_voting.py` | 11 | the 4-vs-1 example, agreement floor, tie-breaks, window decay |
| `test_plate_association.py` | 11 | per-track windows, expiry, reused ids, window overflow |
| `test_anpr_pipeline.py` | 13 | stubbed engine: no plate, empty OCR, invalid text, low score, two vehicles, publish-once, `enabled: false`, `reset()` |
| `test_anpr_config.py` | 17 | YAML -> config -> detector decision, defaults, invalid values rejected at load time, and the no-YAML-imports-in-anpr guard |

### Phase 5 limitations

- **No field validation of OCR.** There is no recording on this machine with a
  legible plate, so per-frame and stable-plate accuracy on a real gate are
  unmeasured. Every accuracy figure above is from rendered plates and
  characterises the software, not a deployment.
- **Plate pixel width is the whole game.** Below 110 px the characters cannot be
  resolved (2-5% exact at 30-45 px). At 150 px the best single frame here is 55%
  exact. This is why the stage defaults to rejecting rather than guessing.
- **Camera distance.** Everything further than the width floor simply yields no
  candidate: 16/40 vehicles in the replay and 748/748 on the real clip. Silence
  is the correct behaviour, but it does look like "ANPR is broken" to an operator
  who has not run `measure_plates.py`.
- **Camera angle.** Tilt is the worst degradation measured (16% exact, against
  32% clean). A gate camera mounted high and looking down at the plate's top
  edge is the failure mode to avoid.
- **Motion blur.** 20% exact on blurred fixture plates; a car rolling through a
  gate at walking pace is enough to produce this. The mitigation is shutter
  time on the camera, not software.
- **Lighting.** Not exercised by the fixture beyond noise; headlight glare,
  night IR and washed-out plates need a real recording to quantify.
- **Compression.** JPEG-compressed fixture plates read at 32% exact against 32%
  clean, so moderate compression is not the limiting factor at these widths -
  but the clip's 86 px plates leave no margin to test that at low resolution.
- **Resolution of the source matters more than the model.** 480x360 cannot be
  tuned into an ANPR camera; 1080p at the same framing would put the same plate
  in the readable band (projection, not measurement).
- Detector recall is 69% on the fixture even when every screen is relaxed, so a
  plate that is *legible* is not automatically *found*.
- Two-line plates are excluded by the aspect screens (`min_aspect_ratio: 1.4`),
  as are heavy oblique angles; both are deliberate and documented in the config.



## Phase 6 - gate lines and parking sessions

Occupancy answers "is this bay taken?". It cannot answer "was this car here for
47 minutes, and what was its plate?" - that is a second camera with a different
job: a line a vehicle crosses going in, a line it crosses coming out, and a
record that joins the crossing to the plate Phase 5 reads. This phase builds
exactly that - geometry, direction logic, the session state machine and the
track-to-plate binding - in memory. No database, no API, no billing.

```
gate frames -> vehicle tracks (ByteTrack id)
  -> GateLogic       this frame's centroid segment vs each line segment,
                     direction from the cross-product sign, zone filter,
                     dwell timer for vehicles that simply vanish
  -> GateCrossing    camera, track, line, role, direction, timestamp, centroid
  -> SessionService  joins the plate read for that track id, opens/closes a visit
  -> VEHICLE_ENTERED / VEHICLE_EXITED / VIOLATION
```

Modules: `parking/gate_logic.py` (geometry only - it has never heard of plates
or sessions), `services/session_service.py` (the first module in `services/`,
because this is the seam the architecture doc describes: it consumes the AI
side's events and produces the business side's records),
`pipeline/gate_pipeline.py` (the loop; owns neither the crossing rules nor the
session lifecycle), `tracking/replay.py` (labelled boxes instead of YOLO),
`scripts/run_gate.py` (the CLI, including the ground-truth scorer), `draw_gate`
in `parking/render.py`, and `GateCollector` / `interactive_gate` in
`parking/calibration.py`.

### Configuration

```
Config file:    configs/app.yaml    -> gate:      timers, shared by every gate camera
Geometry file:  configs/gates/*.json -> lines + zone, one file per camera
Wiring:         configs/cameras.yaml -> per-camera gate_file:
Loader:         core/config.py :: gate_config_for(cfg, camera) -> GateConfig
Consumers:      GatePipeline.__init__ -> GateLogic(gate, camera.id)
                                           SessionService(gate, bus)
```

The rule from Phases 4 and 5 still holds and is still tested: `gate_logic.py`,
`session_service.py` and `gate_pipeline.py` never open a YAML or JSON file -
they are handed a `GateConfig`. `gate_pipeline.py` was added to both existing
guard tests when it was written, because the pipeline drives both stages. The
calibration tool is the one place that does write geometry files, and it is a
human-invoked script, not part of the running pipeline.

```yaml
gate:
  enabled: true
  dwell_exit_s: 12.0        # unseen inside the exit zone for this long -> it left
  track_timeout_s: 5.0      # longer silence is a new vehicle, so a recycled
                            # ByteTrack id may fire an entry again
  min_ocr_confidence: 0.80  # below this the session is kept but flagged for review
```

A camera with no `gate_file:` gets the timers and no lines, which means the
stage stays **inert** rather than guessing where the barrier is - same philosophy
as `mark_all()` for a dead camera. Geometry comes from the file, timers from
YAML, and two cameras never share one file's lines (`test_gate_config.py`).

### Crossing: the segment, not the side

The obvious implementation asks whether the centroid is on the other side of the
line this frame. It is wrong twice over: a vehicle moving fast enough can be on
the far side in the next frame without any frame ever landing on the line, and a
car stopped at a barrier with a 2 px centroid wobble fires an entry every frame.
So each frame's motion is a **segment** and it must *properly straddle* the line
segment - which catches the fast car - while the cross-product sign still gives
the direction, and each track fires each role at most once, keyed by
`(camera_id, track_id, role)` - which kills the wobble. A centroid sitting
exactly on the line is deliberately not a crossing; requiring both segments to
actually intersect is what makes that consistent instead of a coin flip.

**Direction convention** (image coordinates, y downwards): for a line drawn left
to right, traffic travelling *down* the frame goes from side -1 to side +1, so
its crossing is `"positive"`. A gate whose traffic flows the other way says
`direction: negative`, or the operator flips the line with `d` in the calibration
tool. `configs/gates/gate_a.json` is the case the convention has to be able to
express: two lines on the *same segment* with opposite directions, because the
only gate-style clip on this machine is a two-way road.

**Two kinds of exit.** Crossing the exit line (`exit_reason: "line"`), and
disappearing while its last sighting was inside the exit zone (`"dwell"`). The
second is how a barrier exit usually looks - the car leaves the picture, it does
not cross a drawn line - and it is keyed on "not seen anywhere", not "left the
zone", so a vehicle still visible outside the zone is not an exit. The exit is
timestamped at the **last in-zone sighting**, not at the frame the timer fired,
so the duration is the visit rather than the timeout. Sightings are kept as a
small list per track so a ByteTrack id recycled inside the window cannot
overwrite a pending exit.

### Sessions

`ParkingSession` carries `session_id, camera_id, track_id, plate,
ocr_confidence, entry_at, exit_at, exit_reason, duration_s, slot_id, status,
review_reason, anomaly_reason`. `status` is the lifecycle
(ACTIVE / COMPLETED / NEEDS_REVIEW / ANOMALY) and the reasons are flags, kept
separate on purpose: a car that has entered but not exited is ACTIVE whether or
not its plate is legible, and "look at this one" survives closing - which is why
a session can end NEEDS_REVIEW and still hold a real duration.

| Situation | Behaviour |
| --- | --- |
| entry crossing | one ACTIVE session; a plate already read for this track binds here |
| plate read *before* the line | stashed per `(camera, track)`; consumed at entry, or expires after `track_timeout_s` instead of reaching the next car |
| plate read after the entry | binds to the open session |
| same track re-crosses the entry line | ignored and counted, never a second session |
| exit crossing | closes with a duration; NEEDS_REVIEW `no plate read` if the plate never came |
| OCR confidence below 0.80 | session kept and flagged, not silently accepted |
| plate changes mid-visit | bound **and** flagged `plate changed` |
| exit with no entry | ANOMALY + VIOLATION `exit_without_entry`, and **no duration is invented** |
| second ACTIVE session for one plate | ANOMALY `duplicate_active_session` |
| one track id on two cameras | two sessions - the key always carries `camera_id` |

`slot_id` stays `None` and `bind_slot()` is never called by any pipeline: linking
a visit to a bay is a later phase's decision, and the test that pins it
(`test_slot_binding_is_explicit_and_optional`) exists so it stays optional.

### Calibrating a gate

```bash
py -3.10 scripts/calibrate.py --gate --source <clip> --out configs/gates/gate_b.json
```

`i` starts an entry line and `o` an exit line (then click both ends), `z` starts
the dwell zone and `n`/Enter closes it - fewer than 3 clicks warns and refuses;
`d` flips the last line's direction, `u`/`x`/`c` undo, delete last, clear,
`s` saves, `q` quits. Lines are drawn as
**arrows**, because direction is a property of the order the two endpoints were
clicked, which is invisible otherwise. Saving refuses a drawing with no lines -
a gate file with only a zone is not a gate. Coordinates are stored normalised
0-1 like the slot files, so one drawing survives a resolution change;
`test_the_same_drawing_works_at_another_resolution` draws the same gate at
480x360 and 1440x1080 and compares the saved numbers.

### Test results

87 gate tests on top of the 147 from Phases 1-5 - **234 passing**, none of the
existing ones edited, weakened or skipped to make the new code pass.

| file | n | covers |
| --- | --- | --- |
| `test_gate_logic.py` | 22 | side convention, proper straddle, one fire per role, jitter at the line, re-entry after leaving, wrong-way traffic, disabled gate, a fast vehicle that skips over the line between frames, zone filter, dwell exit (fires once, is not raised for a vehicle still visible, is not raised for a track that never entered), `track_timeout_s` letting a recycled id fire again, video-time stamps, the same drawing at another resolution |
| `test_gate_sessions.py` | 24 | entry -> one session, full visit with duration, plate before/after the line, repeat entry ignored, a second visit after an exit, two cameras, dwell reason, duplicate ACTIVE per plate, exit-without-entry carrying **no** duration, a dwell exit dated *before* its own entry as `exit_before_entry` with no duration, weak plate flagged with the threshold from config, no-plate session flagged at exit, `plate changed`, pending-plate expiry vs in-time bind, optional slot binding, `reset()`, working without a bus, and the no-database / no-API import guard |
| `test_gate_config.py` | 19 | shipped `gate:` block and shipped geometry file validate, a lot camera stays inert, geometry from the file + timers from YAML, two cameras not sharing lines, pixel coordinates rejected, role required, unknown role rejected, endpoints checked, unique line names, zone needs a real polygon, review threshold is a probability, timers positive, a file with no lines loads empty, and the no-YAML-in-gate-modules guard |
| `test_gate_pipeline.py` | 11 | one visit produces one duration; a re-crossed entry line never opens a second session; a vehicle outside the zone produces nothing; a vanished vehicle closes on a *skipped* frame and keeps its last-sighting timestamp; a plate read before the line binds at entry; a low-confidence read lands NEEDS_REVIEW; the overlay labels tracks by session state; replay mode never builds a detector even with `track=True`; the pipeline hands **the same** `GateConfig` object to both stages; crossings without ANPR still run sessions |
| `test_gate_calibration.py` | 12 | line naming by role, clicks while nothing is being drawn, resolution independence, `d` affecting only the last line, undo / delete-last, a 2-point zone refused, the saved payload validating into `GateConfig` with both roles, save -> `load_gate_lines` round trip, a saved file picking up the global timers, an empty drawing loading as inert geometry while a malformed line raises `ValidationError`, and rendering the canvas without a window |

### Acceptance on the labelled synthetic clip

The doc's criterion is *"scripted clips with known times; test jitter and
re-entry. No duplicate events; durations within a few seconds of ground truth."*

`scripts/make_gate_fixture.py` renders `data/fixtures/gate_visit.mp4` (90 frames,
480x360, 15 fps): five vehicles, each one a deliberate failure mode, with the
frame every event must land on written into
`data/fixtures/gate_visit_tracks.json` next to the ground-truth plate. The
vehicle boxes are replayed rather than detected (`--tracks`,
`tracking/replay.py`) for the same reason Phase 5 has a fixture: YOLO cannot see
a drawn car, and a gate stage with no tracks has nothing to cross a line.

`py -3.10 scripts/run_gate.py --fixture`:

| track | designed | want in / out / dwell | observed in / out | duration | plate |
| --- | --- | --- | --- | --- | --- |
| 1 | crosses in, waits, crosses back out | 16 / 60 / - | 16 / 60 | **2.93 s** by line (designed ~2.9 s) | `MH20AB1234` correct |
| 2 | wobbles +/-5 px as it crosses the barrier | 21 / - / - | 21 | still ACTIVE (never leaves) | `TS09DP4567` correct |
| 3 | crosses in, then drives out of frame right | 15 / - / 43 | 15 / 43 | **1.87 s** by dwell | `DL12CD8901` correct |
| 4 | only ever crosses right-to-left | - / 76 / - | - / 76 | none, by design | `KA01MN2345` correct |
| 5 | runs along the top edge, outside the zone | nothing | nothing | - | `GJ18BB5678` read, no session |

```
frames 90  crossings 6  repeat crossings ignored 0  duplicate entries refused 0
sessions 4 (1 still open)   events on the bus: 13
3 entered, 2 exited, 1 violations, 1 anomaly sessions
5 of 5 fixture vehicles behaved exactly as designed
```

(The 6 crossings are the 5 drawn line events plus track 3's dwell exit, which is
also a `GateCrossing`; the 13 bus events are those 6 session events plus 7
`PLATE_READ`s, track 2 contributing three because its vote moved and moved back.)

Every entry and exit landed on its expected **frame**, not merely within the
2-frame tolerance. Track 3's exit is stamped 43 while the track is still
partially visible until 55 - that is the last-in-zone-sighting rule working, and
it is why the duration reads 1.87 s instead of the 2.0 s dwell timer. Track 2
shows the plate layer refusing to pick silently: part-way through the visit OCR
re-voted to `TS0QDP4567` (an 8/Q confusion, 5 of 7 agreeing) and then back to
`TS09DP4567`, and the session records the change as `plate changed` while
staying ACTIVE.

With `--no-anpr` the same clip gives the identical 5/5 verdict and 6 bus events
instead of 13, which is what separates the geometry work from the OCR work in
the numbers below.

**This is a synthetic fixture: it validates the software chain, not a gate.**

### Real footage

`py -3.10 scripts/run_gate.py --camera gate_cam1 --max-frames 450 --stride 3`
(YOLO + ByteTrack on `data/samples/gate_traffic.mp4`, the shipped demo
calibration):

```
frames 450  crossings 7  repeat crossings ignored 0  duplicate entries refused 0
sessions 6 (2 still open)   events on the bus: 7
3 entered, 1 exited, 3 violations, 3 anomaly sessions
throughput 20.1 frames/s overall (8.2 s model warm-up), 31.8 frames/s steady
```

All six sessions are plateless, so the closed ones end NEEDS_REVIEW or ANOMALY -
correct for a clip Phase 5 measured at an 86 px best plate. The 7 crossings are
3 entries plus 4 exits, and 3 of those exits had no matching entry, because this
clip is a two-way public road: vehicles genuinely appear first at the exit line.
Read that as *"the calibration is a demo"*, not as a bug - `gate_a.json`'s own
notes record what was measured there (4 tracks left-to-right, 4 right-to-left,
13 of 31 tracks long enough to judge barely moving). A single-direction barrier
drawing does not produce it.

**There is no real gate-camera recording on this machine**, so session durations
on a real visit remain unmeasured - the same field blocker Phase 5 documents.

### Performance

| Configuration | Measured |
| --- | --- |
| fixture replay, geometry + sessions only (`--no-anpr`) | 2638 frames/s overall, **3560 frames/s steady** - the whole 90-frame run costs ~34 ms, so the gate stage is not a bottleneck at any frame rate |
| fixture replay + CPU OCR (the acceptance run above) | **1.9 frames/s**, 2.2 s warm-up included |
| real footage, YOLO + ByteTrack, stride 3, ANPR enabled | 20.1 frames/s overall / **31.8 frames/s steady** |

The 1.9 is the OCR stage, not the gate - the identical frames take milliseconds
without it. The real-footage number moves a lot on this laptop: the RTX 4050
idles at ~600 MHz between runs, and repeated runs of the same command have
reported 31.8 to 102.8 frames/s steady within this phase. Warm-up (8.2-8.9 s) is
reported separately from steady state in every run, never folded into one
average.

A gate camera does not need 25 sessions per second. One vehicle needs one entry
crossing and one exit crossing; `--stride 3` is the knob when the same box is
also running the lot pipeline.

### Phase 6 limitations

- **Sessions bind to synthetic/replayed plates.** Accepted before starting: until
  a real entrance clip exists, the plate inside a session comes from rendered
  plates. Nothing about a real gate visit is measured end to end.
- **`gate_a.json` is a demo calibration** drawn on a road clip, not a barrier.
  Redraw per camera with `scripts/calibrate.py --gate` before trusting an entry
  or exit count from a deployment.
- **`dwell_exit_s: 12.0` is a starting value, not a measured one.** The fixture
  run overrides it to 2.0 s because a 12 s timer could never fire inside a 6 s
  clip; the real number needs a recording where departures are timed.
- **Everything rests on track identity.** If ByteTrack swaps an id, the "same"
  car can re-enter, and that re-entry is *suppressed* by the idempotency key
  rather than opening a second session - the safe direction (a visit that stays
  ACTIVE and gets flagged rather than a phantom booking) but not the correct
  one. Per-plate uniqueness in Phase 7 is what surfaces it. A swap on real
  footage was measured, and Phase 7 found its second form: see
  `exit_before_entry` in the Phase 7 limitations below.
- **Durations inherit the frame-rate estimate.** Entry and exit are both video
  time from the same clip, so a variable-frame-rate source distorts them.
- **No persistence until Phase 7.** Restarting the process lost every ACTIVE
  session; `services/persistence.py` now writes them to SQLite through
  SQLAlchemy, and `--persist` on either CLI turns a run into rows.
- **The zone test is a centroid test.** A long vehicle whose body has left the
  zone - or the frame - while its centroid is still inside keeps refreshing its
  last in-zone sighting, so a dwell-stamped exit can be a frame or two late
  relative to the physical departure. The dwell clock itself is keyed on being
  seen *anywhere*, so a vehicle that is merely outside the drawn zone is never
  turned into an exit by the timer.

## Phase 7 - database and persistence

Phase 6 ended at a printed table: a visit existed only as long as the process did.
Phase 7 puts the same objects on disk, through SQLAlchemy 2.x and Alembic, on
SQLite - because this machine has no Docker, and the architecture doc's Postgres is
a URL change, not a code change.

```
YOLO -> tracking -> occupancy -> ANPR -> gate crossings -> sessions
    -> EventBus (unchanged) -> services/persistence.py -> repositories -> SQLite
```

The pipeline already published `Event` objects on a bounded bus, so persistence is
**just another subscriber**: nothing under `detection/`, `parking/`, `tracking/`,
`anpr/`, `pipeline/` or `video/` was edited to make this work, and a guard test
(`test_the_ai_layers_still_import_nothing_from_the_database`) fails if one of them
ever imports `smart_parking.db`, `sqlalchemy` or `alembic`. `scripts/run_gate.py
--persist` and `scripts/run_occupancy.py --persist` are the only places a run
touches a database; the modules receive a `DatabaseConfig`, never a path.

### The seven tables

| Table | One row is | Written when |
| --- | --- | --- |
| `cameras` | a configured viewpoint, keyed by the same id `cameras.yaml` uses | `sync_configuration`, before inference starts |
| `parking_slots` | a calibrated bay, its polygon and the status the FSM last committed | same seed, then every committed transition |
| `license_plates` | a plate the Phase 5 validator agreed on, plus how many times it was read | first sighting of that text |
| `vehicles` | a plate plus the class the detector reported for it | a visit that could name a class |
| `parking_sessions` | one visit: crossing in, crossing out, what it joined to | `VEHICLE_ENTERED`, updated by `VEHICLE_EXITED` |
| `slot_occupancy_history` | one stay in one bay - open while the bay is taken, closed when it frees | `SLOT_OCCUPIED` / `SLOT_FREED` |
| `detection_logs` | one meaningful domain event, payload intact | every event except per-frame `SUMMARY` |

```
configs/cameras.yaml ┐  sync_configuration (idempotent)
configs/slots/*.json ┘
        │
        v
     cameras ──────────────┐
       │ 1:N               │ 1:N (camera_id, entry_camera_id, exit_camera_id)
       v                   v
  parking_slots      parking_sessions ──N:1──> license_plates ──1:N──> vehicles
       │ 1:N               │   ↑ plate_id, vehicle_id, slot_id all NULLable
       v                   │
 slot_occupancy_history ───┘ (session_id, when the gate also saw the visit)
       (one per stay)
                       parking_sessions ──< detection_logs >── cameras
                                              (one per event)
```

`slot_id` and `plate_id` are nullable because that is the world the cameras
actually give: a gate camera does not see the bays, and ANPR is not field-validated
yet. `entry_time IS NULL` with an `exit_time` set is the persisted form of an
exit-without-entry - the entry time is never reconstructed backwards from the exit.

### Keys

Primary keys are surrogate integers except `cameras`, whose primary key *is* its
natural key (`camera_id`, `String(64)`), so one id means the same thing in every
table and in every log line. `parking_sessions.source_session_id` is the id the
Phase 6 state machine minted (`gate_cam1-00007`) and is unique, which is what makes
a replayed visit an update rather than a second row.

| Table | Foreign keys | Unique |
| --- | --- | --- |
| `cameras` | - | `pk_cameras` (camera_id) |
| `parking_slots` | `camera_id` -> `cameras` | `uq_parking_slots_camera_id_slot_code` - A1 means A1 *for one camera* |
| `license_plates` | - | `uq_license_plates_plate_text` |
| `vehicles` | `plate_id` -> `license_plates` | `uq_vehicles_plate_id_vehicle_type` |
| `parking_sessions` | `camera_id`, `entry_camera_id`, `exit_camera_id` -> `cameras`; `plate_id` -> `license_plates`; `vehicle_id` -> `vehicles`; `slot_id` -> `parking_slots` | `uq_parking_sessions_source_session_id` + the three partial indexes below |
| `slot_occupancy_history` | `slot_id` -> `parking_slots`; `session_id` -> `parking_sessions` | `uq_slot_occupancy_history_slot_id_started_at` |
| `detection_logs` | `camera_id` -> `cameras`; `session_id` -> `parking_sessions` | - |

SQLite ignores `REFERENCES` until a connection says otherwise, so `build_engine`
sets `PRAGMA foreign_keys=ON` on every connect and
`test_foreign_keys_are_actually_enforced` proves a bad `camera_id` is refused. A
file database also runs `PRAGMA journal_mode=WAL` so the run's reader (the
end-of-run report, later an API) is not blocked by the worker's commit;
`synchronous` is left at SQLite's own default, because losing the last transaction
for a faster commit is not a trade this phase was asked to make.

### The doc's "one ACTIVE session per plate / per slot / per track"

Three partial unique indexes, not application logic:

```sql
uq_parking_sessions_active_track  ON parking_sessions (camera_id, entry_track_id) WHERE status = 'ACTIVE'
uq_parking_sessions_active_plate  ON parking_sessions (plate_id)                  WHERE status = 'ACTIVE' AND plate_id IS NOT NULL
uq_parking_sessions_active_slot   ON parking_sessions (slot_id)                   WHERE status = 'ACTIVE' AND slot_id IS NOT NULL
```

Partial indexes were assumed to be a Postgres feature; SQLite has had them since
3.8, so the rule is enforced and tested here rather than deferred to a database
nobody runs yet. A duplicate *live* claim for one plate is not silently rejected:
the state machine marks the newcomer `ANOMALY`, which is not `ACTIVE`, so both rows
exist and the operator decides which visit is real.

### Other constraints, and why each is there

| Constraint | Rule |
| --- | --- |
| `ck_parking_sessions_duration_is_not_negative` | a visit does not last less than nothing |
| `ck_parking_sessions_exit_is_not_before_entry` | the two timestamps of one visit are ordered |
| `ck_parking_sessions_completed_needs_both_times` | only `COMPLETED` needs both; `ANOMALY` keeps whichever half was measured |
| `ck_parking_sessions_exit_reason_is_known` | `line` or `dwell`, because that is all the gate logic can distinguish |
| `ck_parking_slots_slot_status`, `ck_cameras_camera_role`, `ck_parking_slots_slot_type`, `ck_parking_sessions_session_status` | the vocabulary is a database rule, so a hand-written row cannot invent a state the FSM never defined |
| `ck_vehicles_vehicle_type_is_a_detected_class` | `car`, `motorcycle`, `bus`, `truck` - the COCO classes the detector reports |
| `ck_slot_occupancy_history_interval_is_ordered` | a stay does not end before it starts |

Indexes for the questions an operator (and Phase 8's API) will actually ask -
today's occupancy, visits in a time range, one camera's trail:
`ix_parking_sessions_status`, `ix_parking_sessions_entry_time`,
`ix_slot_occupancy_history_slot_started`, `ix_detection_logs_camera_ts`,
`ix_detection_logs_event_type`.

### The seam: `services/persistence.py`

* **A thread, not the frame path.** The worker drains the existing bus on its own
  thread; inference never waits for a commit.
* **One transaction per batch.** A plate, a vehicle and a session written in the
  same batch either all appear or none do. `drain` returns as soon as it holds one
  event, so the worker waits a bounded `linger` (0.1 s) for the batch to fill -
  without it a 148-frame lot replay committed one transaction per frame (measured:
  151 events in 150 transactions), and a synthetic 2000-event run measured 600
  events/s batched against 226 events/s committed per event, about 2.8 ms of
  commit each. The same lot replay now needs 38 transactions.
* **`SUMMARY` is consumed, not stored.** It is a per-frame counter snapshot; a row
  per frame is the one thing this database is documented as not being. On the lot
  replay 151 events arrived and 16 were logged.
* **Failures are loud.** A rejected batch is retried one event at a time so one bad
  event cannot roll back the good ones; the surviving error is logged with its
  traceback, kept in `worker.failures`, and printed by the CLI under
  `PERSISTENCE INCOMPLETE`. Nothing is swallowed.
* **`stop()` drains the tail** after joining its thread, in the calling thread, so
  the run's last visit is in the database before the report is printed.
* **Timestamps convert once.** The domain keeps epoch floats; every datetime stored
  is naive UTC, and `db/base.py` is the only place that crosses that line.
* **A ByteTrack id is not an identity.** It is stored as `occupied_by_track_id` and
  `entry_track_id` for review, never as the answer to "which car". What survives a
  sighting is the plate.
* **A second run of the same clip does not rewrite the first one.** Because
  `source_session_id` is unique, replaying `gate_visit.mp4` into a file that already
  holds its visits updates the identity rows and appends to `detection_logs`, and
  refuses the second `vehicle_exited` for each already-closed session: measured, 11
  of 13 events handled, 2 refused and printed, the first run's entry/exit times and
  durations left exactly as they were, log rows 13 -> 24. The session id is
  per-camera and sequential, not per-run, so `--persist` into a shared file needs a
  run-scoped id (or a fresh file) to record a re-run as a second visit.

Deliberate deviations from the doc: plates are stored as **normalised text**, not
AES-GCM ciphertext with an HMAC lookup hash, because there is no secret store on
this machine and an encrypted column with a hard-coded key is a false claim of
protection; geometry and payloads are `JSON`, not Postgres `JSONB`; and `users`,
`reservations`, `tariffs`, `payments` and `audit_logs` are not created yet, because
nothing writes a sensible row into them before the authentication and billing
phases.

### Migrations

Alembic owns the schema. A run never creates tables: `run_gate.py --persist`
checks for `parking_sessions` and tells you to migrate first rather than quietly
building a second, unversioned schema.

```bash
py -3.10 scripts/migrate.py current              # what this file is at
py -3.10 scripts/migrate.py upgrade head         # create / update it
py -3.10 scripts/migrate.py check                # models vs database, must be empty
py -3.10 scripts/migrate.py revision --autogenerate -m "add tariffs"
py -3.10 scripts/migrate.py downgrade -1         # reversible; prints the URL first
py -3.10 scripts/migrate.py upgrade head --url sqlite:///data/scratch.db
```

`env.py` resolves the URL through the same `database_url_for` the application uses,
so migrations and runtime cannot drift to two different files; `--url` and
`SMART_PARKING_DATABASE_URL` are the overrides. The initial migration is `0001`,
`render_as_batch` is on (SQLite cannot `ALTER COLUMN`), and
`test_the_migrated_schema_matches_the_models_exactly` diffs the migrated database
against the ORM metadata column by column, index by index, constraint by
constraint - a migration nobody diffs is a second description of the database
waiting to drift.

### What a run actually writes

Measured on this machine, 2026-09-30, all with `--persist` against a freshly
migrated file database:

| Run | Events | Rows written |
| --- | --- | --- |
| `run_gate.py --fixture` (labelled synthetic clip) | 13 of 13 handled, 0 dropped, 12 transactions | 4 sessions, 6 plates, 6 vehicles, 13 log rows; durations 1.867 s and 2.933 s exactly as the console printed, 1 `ACTIVE` (plate changed), 1 `ANOMALY` (exit_without_entry, `entry_time` NULL) |
| `run_occupancy.py` (148-frame lot clip, 5 bays) | 151 of 151 handled, 0 dropped, 38 transactions | 5 bay rows, 3 stay intervals (all still open - nothing freed during the clip), 16 log rows. The other 135 events are per-frame `SUMMARY` snapshots and wrote nothing |
| `run_gate.py --camera gate_cam1 --no-anpr` (real road clip, ~8.5 k frames) | 609 of 609 handled, 0 dropped, 373 transactions | 578 sessions: 29 visits that closed but have no identity (`NEEDS_REVIEW`) and 549 anomalies (547 `exit_without_entry`, 2 `exit_before_entry`), every one with `plate_id` NULL because no plate on that clip reaches the 110 px readable floor; 609 log rows (31 entries, 29 exits, 549 violations). 572 KiB |
| synthetic 2000-occupancy-event batch | one transaction: 3.3 s (600 events/s); one per event: 8.8 s (226 events/s) | 4000 log rows + 2000 intervals, 1.2 MiB |

Row volume, extrapolated by arithmetic rather than measured: a 200-visit day at one
gate is ~200 session rows, ~200 stay intervals and a few hundred log rows, so
weeks rather than months of retention fit in a few megabytes, and no retention job
is needed yet.

The throughput cost of writing is inside the noise of this GPU. Three runs of the
lot clip with `--persist` and three without reported overlapping `mean_fps`
(14.3-20.3 with, 12.0-18.2 without); the `steady_fps` spread (20-50) is the laptop's
clock drift, which is far larger than the commit cost the benchmark isolates.

### Phase 7 limitations

- **Inherited field block.** The only real gate footage here has 86 px plates
  against a 110 px floor, so every persisted session from it has `plate_id` NULL.
  The database is proven on the synthetic clip, where the plates are rendered; that
  exercises the chain, it does not measure ANPR in the field.
- **`exit_before_entry` is a track-identity failure the schema caught.** On the
  road clip a dwell exit arrived stamped at the vehicle's last sighting in the zone,
  which was four frames *before* the entry the same track id had just opened. The
  ordering CHECK refused the row - correctly - so the state machine now records it
  as an anomaly with the entry kept, no exit stamped and no duration, and the event
  log holds the claimed time. `test_a_dwell_exit_dated_before_its_own_entry_is_an_anomaly_not_a_negative_visit`
  pins it. A negative duration is not rounded up to zero.
- **One writer, one reader.** Only the worker thread writes; the calling thread
  seeds the configuration before it starts and reads the run's rows after
  `stop()` has drained the queue, so the two never write at once - which is what
  `check_same_thread=False` is conceding, not a new concurrency claim. WAL keeps
  that reader from waiting on a commit. Two processes writing one file is a
  Postgres question, not a knob to turn on here.
- **No plate encryption yet**, for the reason above: the `hash` column is where the
  doc's HMAC lookup goes once a key store exists.
- **`slot_id` is never filled by a run.** The lot camera reports lot track ids and
  the gate camera reports gate track ids; joining them needs one camera seeing both
  or cross-camera plate matching (doc Phase 10). The column and its index exist;
  nothing honestly answers the question yet, so nothing writes a guess into it.
- **`vehicle_type` is only as good as the detection that carried the plate**, and
  `is_registered` is `False` everywhere until the accounts phase has a registry to
  say otherwise.

## Phase 8 - FastAPI: REST surface, JWT/RBAC, WebSocket hub, MJPEG

The doc's section E is a table of endpoints, a table of WebSocket channels and one
line about security. This phase builds the operational half of that table - the
routes that read and change what Phases 1-7 already produce - and refuses the
billing half rather than inventing it. Four scope answers were fixed before the
first line was written, and each one is still visible in the code:

* **Core only, no billing.** No reservations, payments, tariffs or violations
  tables. `GET /stats/revenue` exists as the doc names it and answers **501
  `not_implemented`**, because a `0` there would be read as a measurement.
* **The migration creates `users` + `audit_logs` and nothing else** (`0002_…`), so
  `alembic upgrade head` on a Phase 7 database turns it into a Phase 8 one without
  touching the seven rows that are already there.
* **The signing key comes from an env var, with a dev bootstrap file.** Nothing in
  `configs/` holds a secret; `jwt_secret_for` raises if it cannot find one.
* **The video is replay clips, started by the app's own lifespan.** This machine
  has no camera and no Docker, so `create_app` boots the two clips that fit in 6
  GB of VRAM and `/health` reports what they are doing.

### The surface

REST lives under `api.prefix` (`/api/v1`). The two WebSockets are mounted at the
root, exactly as the doc writes them (`WS /ws/live?token=`), because a dashboard
builds a socket URL from its own page URL, not from an API base - a detail worth
knowing before `new WebSocket('/api/v1/ws/live')` returns a 4401 close.

| Endpoint | Needs | Notes |
| --- | --- | --- |
| `POST /auth/login` | - | email + password -> access + refresh. The only unauthenticated route |
| `POST /auth/refresh` | refresh token in the body | a new pair; an *access* token here is a 401 |
| `GET /auth/me`, `GET /users/me` | any role | identity, plus whether this caller sees full plates |
| `GET /users`, `POST /users`, `POST /users/{id}/deactivate` | ADMIN | accounts are deactivated, never deleted |
| `GET /health` | any role | db, GPU, per-camera live state, counters, uptime |
| `GET /cameras`, `GET /cameras/{id}` | any role | stored rows merged with what the runner says right now |
| `POST /cameras` / `PATCH /cameras/{id}` | ADMIN / ADMIN+OPERATOR | both audited |
| `POST /cameras/{id}/restart` | ADMIN+OPERATOR | stops the thread and starts a new one |
| `GET /cameras/{id}/snapshot`, `.../stream.mjpg`, `.../overlay` | any role | `<img>`-friendly: header **or** `?token=` |
| `GET /slots`, `GET /lots/{camera_id}/summary` | any role | the doc's `{total, occupied, available, reserved, unknown}` |
| `POST /slots` | ADMIN | polygon stored normalised, like a calibration file |
| `PUT /slots/{id}/polygon` | ADMIN+OPERATOR | audited |
| `GET /sessions`, `GET /sessions/{id}` | any role | filters `status, plate, camera_id, from, to, limit, offset` |
| `PATCH /sessions/{id}/plate` | ADMIN+OPERATOR | the manual correction, audited with both sides masked |
| `POST /sessions/{id}/close` | ADMIN+OPERATOR | force close, audited |
| `GET /plates/search?q=` | any role | **masked for every role**, and a fragment shorter than three normalised characters is a 422 |
| `GET /stats/occupancy`, `GET /stats/duration` | any role | default window 24 h, cap 92 days |
| `GET /stats/revenue` | any role | 501, by design |
| `GET /audit-logs` | ADMIN | the doc's name for it; filters `action, actor_email, from, to` |
| `WS /ws/live?token=` | any role | slot / summary / session / violation / plate / camera channels, one JSON per event |
| `WS /ws/cameras/{id}/overlay?token=` | any role | newest frame per tick, boxes for a canvas |

Not built, because they are a later phase's product decision rather than an
oversight: `/vehicles`, `/reservations`, `/payments`, `/tariffs`, `/violations` and
the `images`/`payment` fields the doc puts in a session detail.

### Every answer has the same shape

One envelope for everything that went wrong - `ApiError`, a `CredentialError`, a
FastAPI validation failure, an `IntegrityError` from a repository, and a URL that
matches no route (404 `route_unknown`, JSON, not an HTML page):

```json
{ "error": { "code": "role_not_allowed", "message": "this endpoint needs ADMIN, not viewer" } }
```

Five handlers, one shape, because a dashboard that has to parse two error formats
ends up parsing neither. `code` is the machine-readable half a client branches on;
the HTTP status is the coarse one.

### Who may call what: Argon2id, one short token, four roles

**Passwords.** Argon2id, at argon2-cffi's own recommended cost (64 MiB, 3 passes,
4 lanes) - measured here at **50-65 ms to hash, ~50 ms to verify**. The cost lives
in `api.password` because it belongs to the hardware, and a test suite that hashes
a password forty times asks for the cheap end instead. A password must clear a
length policy (10-128) *before* any hashing, and one that contains its own address
is refused: a parking system's first operator is quite likely to be
`admin/admin`.

**Logins that fail get one answer.** "No such account", "wrong password" and
"deactivated" all return 401 `bad_credentials`, because the difference is a
user-enumeration oracle and a person who mistyped their own email learns nothing
from it. The distinction is kept where it is useful - in the `login_failed` audit
row's `detail.reason`.

**Tokens.** HS256 JWTs, `exp`/`iat`/`sub`/`use`/`role` required on read. Access
900 s, refresh 43 200 s (one shift), both signed by the same key and separated
only by the `use` claim - which is why `POST /auth/refresh` with an access token is
a 401 `token_wrong_use` rather than a free second session. `jti` is present so
revocation has a handle to name.

**Roles are read from the row, not the token.** Authentication re-loads the
account on every request: a deactivated account is 401 `user_inactive`, and an
account whose role changed since the token was minted is 403 `role_changed` rather
than a demoted operator keeping admin powers for another fifteen minutes. The four
roles map onto three dependency tiers - any role for reads, `ADMIN+OPERATOR` for
the writes that change what an operator sees on the board, `ADMIN` alone for the
ones that change the system's shape (accounts, cameras, slots).

### Plates are masked unless a person is being held to them

`mask_plate` keeps the region and the tail (`TS09DP4567 -> TS09**4567`). Only
`ADMIN` and `OPERATOR` get the full number, and only on a session or an overlay:
the reason is `PATCH /sessions/{id}/plate`, where a named person retypes a plate
into a legal record and the audit row can therefore say who. Everywhere else the
rule holds even for those two roles - `GET /plates/search` returns `plate_masked`
and no `plate` for anyone, because a search is a way to walk a table a hundred rows
at a time, and the masked form is still enough to recognise a number you already
know. The audit trail's `detail` stores the before/after **masked**: it is read
back by an ADMIN-facing list, so whatever is written into it is disclosed.

### The live layer: one bus, three subscribers

Phase 7 made the database a subscriber to the event bus. Phase 8 adds two more,
without the pipelines knowing:

```
CameraRunner (thread per camera, paced)
   -> FrameHub        latest FrameView per camera  -> /snapshot, /stream.mjpg, /overlay
   -> WebSocketHub    bus callback, on the inference thread
        -> per-connection bounded queue (api.ws_queue_size) -> /ws/live
   -> PersistenceWorker (Phase 7, only with --persist)
```

* **`FrameHub`** holds one frame per camera and encodes a JPEG only when someone
  asks, at `api.mjpeg.max_width` (960 px here) - a stream is a picture a browser
  keeps, not evidence, so 1080p is not worth two megabytes a second to 40 tabs.
  `jpeg_since(seq)` lets an MJPEG response skip whatever a viewer already has
  rather than re-send it, and the per-viewer rate is `api.mjpeg.fps` (5/s).
* **`WebSocketHub`** is a plain bus subscriber, so it runs on the inference thread
  and must never block or raise: it hands the payload to
  `loop.call_soon_threadsafe`, which is the only legal thing to do from a thread
  that is not the event loop's. Each connection has a bounded queue; when it fills,
  the oldest unread event stays unread and `dropped` goes up. **Inference never waits
  for a browser.** A socket that dies mid-send has its undelivered events counted
  once, not twice.
* The payload is `type` (the bus's own vocabulary - `slot_occupied`, not the doc's
  illustrative `slot_update`) plus a `channel` field for filtering, so a client
  that only cares about bays filters instead of enumerating event types. Renaming
  the contract at the last hop is how a dashboard ends up parsing names that appear
  nowhere else in the system.
* **`CameraManager`** starts the cameras named in `api.pipeline.cameras` from the
  lifespan, and `POST /cameras/{id}/restart` replaces one runner's thread. Every
  thread is stopped and joined before shutdown finishes.

### What a loop does to a database, and what the runner does about it

A served replay has no frame rate of its own until it is given one. Unpaced, a
`while cap.read()` loop runs as fast as the decoder can manage: on this machine
that meant a flickering picture, a pinned core, and hundreds of events a second
arriving at a dashboard nobody was watching. `api.pipeline.pace_replay: true` makes
each runner hold a **running deadline** (`next_due += seconds_per_frame`) and sleep
to it. When the deadline slips, the runner moves it rather than dropping frames -
`elif delay < -0.5: next_due = time.time()` - because on a gate a skipped frame is a
crossing that did not happen.

`api.pipeline.first_pass_events_only: true` is the other half. A session is keyed
on the run's own id, so the second pass of the same clip is the same cars doing the
same thing again, and the database refuses every one of those events - correctly,
and forever. The runner therefore puts a `QuietBus` in front of the pipeline on
every pass after the first: **the frames keep streaming, the events stop being
published**. Two things follow, and both are reported rather than hidden:

* `/health` and `/cameras` show `passes` as the runner's own count. For a **gate**
  camera that number climbs, because the loop is `CameraRunner._run`. For a **LOT**
  camera it stays **0** while `frames_processed` climbs without limit, because that
  rewind lives inside `LotPipeline.run()`. It is not a stall; it is two different
  loops, and the fields that describe them are different.
* A LOT camera's *events* are not actually capped by the runner, because there is
  never a second pass for it to cap. In practice that turns out to be harmless -
  measured across 13 000+ frames past the first loop, the occupancy engine's state
  spans the rewind and only `SUMMARY` was published: no new session, no new
  detection log, no new interval. The claim that it *cannot* re-publish is not made
  anywhere; what is made is that it did not.

### Every write leaves a trail

`audit_logs` is append-only, indexed on `(created_at)` and `(actor_user_id,
created_at)`, with the action constrained to the eleven the code can actually
write: `login`, `login_failed`, `user_created`, `user_deactivated`,
`plate_corrected`, `session_force_closed`, `camera_registered`, `camera_updated`,
`camera_restart`, `slot_created`, `slot_polygon_updated`. `actor_email` is stored
beside the FK on purpose - a failed login has no user row to point at, and a
deleted account must not turn its own history into NULL. The `PATCH /sessions/{id}/plate`
audit row is the one place a correction becomes traceable, and it is why that
endpoint exists at all: an OCR read is a guess about a car, and a person is allowed
to say otherwise.

### Serving it, and where the key comes from

`create_app(cfg, database, bus=…, secret=…, persist=…, start_pipelines=…)` is the
whole wiring in one function, and every one of those arguments is a knob a test
turns: no test reads `data/.secret`, and no test starts YOLO unless it means to.
The lifespan then does four things in order - start the persistence worker if asked,
start the camera runners, yield, and on the way out stop the runners, stop the
worker, log what it wrote. `worker.stop()` joins the thread and *then drains again*,
so a visit published between the stop button and the thread's last breath is in the
database afterwards rather than lost; `test_a_persisting_app_writes_the_visit_that_was_still_on_the_bus_when_it_stopped`
pins exactly that, and it is the test that found a real defect (`app.py` built its
bus with `bus or InMemoryEventBus()`, and an *empty* bus is falsy through
`InMemoryEventBus.__len__`, so an injected bus was silently discarded).

The signing key is resolved before the app is served, on the thread that started
it: `api.jwt_secret_env` names an environment variable, and `api.jwt_secret_file`
names a file `scripts/dev_secret.py` writes (0600, 48 random bytes, refuses to
overwrite an existing key without `--rotate --force`). A deployment with no key
fails at once, with a message naming both ways to supply one, instead of
half-way through a request that would have had no way to say what went wrong.
`test_no_field_of_api_config_can_hold_a_secret_value` is what keeps the key out of
`app.yaml` as the configuration system grows.

### What a run actually serves (measured on this machine, 2026-10-01)

Two replay cameras on an RTX 4050 6 GB, `serve.py --persist`, `pace_replay: true`,
Argon2 at the shipped cost. This is a 17-minute run, so the first two rows are
steady state; the model was already warm.

| What | Measured |
| --- | --- |
| Gate stream (`data/fixtures/gate_visit.mp4`, 90 frames @ 15 fps, YOLO off, labelled tracks replayed) | **121 passes / 10 769 frames in 730 s = 14.8 fps** - its own source rate, so the pacing is doing its job |
| Lot stream (`data/samples/lot_shot5.mp4`, 148 frames @ 30 fps, real YOLO on GPU) | **21 694 frames in 730 s = 29.7 fps** - again its own source rate, with `passes: 0` as above |
| `POST /auth/login`, correct password | **85-124 ms** (one verify at ~50 ms + a read + an audit insert + signing) |
| `POST /auth/login`, wrong password | 96 ms, and 401 `bad_credentials` |
| `GET /cameras/{id}/snapshot`, `GET /cameras/{id}/overlay` | **5-8 ms** each (one JPEG encode at 960 px, one JSON of the latest frame) |
| `GET /cameras/{id}/stream.mjpg` | **16 JPEGs in 3.1 s** to one viewer = the configured 5/s, and the byte-per-frame cost of `quality: 80` |
| `WS /ws/live`, 20 s | **606 `summary` events delivered, `dropped = 0`**, plus the `hello` frame that names the caller's role and queue size |
| `GET /health` | `status: ok`, database reachable, GPU reported as **"NVIDIA GeForce RTX 4050", 6140 MiB total / 4969 MiB free, torch 2.11.0+cu128, CUDA 12.8** - read off `torch.cuda`, not typed into a file |
| Rows after the whole 17 minutes | **4 sessions, 8 detection logs, 3 occupancy intervals, 16 audit entries, 0 persistence failures** - no row per frame, 21 694 lot frames and ~10 700 gate frames aside |
| Cold start | model warmup happens inside the first pass, so the first seconds are slower than the table above; the numbers are the steady state, not an average over the run |

The two CLIs were re-run after all of this and reproduce their Phase 7 records
exactly (fixture: 13/13 events, 4 sessions, 2.933 s and 1.867 s; lot: 151 events,
5 bays, 3 intervals; `--persist` unchanged), so the API layer added subscribers
without changing the pipeline's behaviour. `pace` defaults to **off** for the CLIs
- a benchmark still wants throughput, a server wants a picture - and
`format_failures` without a `total` means exactly what it meant in Phase 7.

### Tests

**542 pass** (59-62 s): the 309 from Phases 1-7 plus **233** from Phase 8. None of
the earlier ones was edited, weakened, skipped or reinterpreted to make this phase
fit; five of the 233 are in Phase 7 files (4 in the schema/migration file for
`users` + `audit_logs`, 1 in `test_persistence.py` for the capped failure sample),
and the remaining **228** are the four new files:

| File | Tests | What it insists on |
| --- | --- | --- |
| `test_api_config.py` | 40 | `ApiConfig` validation, the shipped `app.yaml` matching the documented defaults, the secret resolution order (env wins, dev file is the fallback, an empty env var falls through, a short key is refused), CORS refusing a wildcard, and the three layering seams (`api/` reads no YAML, opens no session; `db/` does not know the API exists) |
| `test_api_auth.py` | 64 | Argon2 policy and rehash-on-login, token issue/decode/expiry/`use`, refresh with an access token, deactivated accounts, role drift between token and row, `mask_plate`, and the plate-reveal rule per role |
| `test_api_endpoints.py` | 72 | Every route × every role, the pagination caps, the error envelope including the 404-for-a-URL-no-route-matches case, the audit filters, the lifespan's "no lost tail" guarantee with a real worker, and - structurally - that the only routes without a token dependency are the two that issue one |
| `test_api_live.py` | 52 | `FrameHub` sequence/`jpeg_since` behaviour, `WebSocketHub` queue bounds and drop counting, `CameraRunner` start/stop/restart, `QuietBus`, the paced-vs-unpaced pass comparison, the looping-camera "one set of events, many pictures" test, and both sockets' handshake refusals with the right close codes |

Four of these changed source rather than being satisfied by it. Besides the
`bus or …` defect above, the paced-pass test is what pinned the running-deadline
pacing in `pipeline/{lot,gate}_pipeline.py`, and the failure-sample cap in
`services/persistence.py` exists because the first unpaced acceptance run of the
server wrote **793 KB of log in 65 seconds** - 84 KB of it in the first eight, and
about 12 KB every second after that - most of it the same refused-except one
`ValueError` repeating as fast as the decoder could loop the clip. The worker now
keeps the first 200 failures verbatim and counts every one (`failure_total`), and
`stats`/the CLI summary report the
count, so a cap on memory is not a cap on what the operator is told. The fourth is
the route-structure test itself: `routers/__init__.py` states that only `auth` is
unauthenticated, and reading the mounted app against that rule found
`GET /stats/revenue` answering 501 to anyone at all - a stub, but still a statement
that a path exists. It now carries `Depends(deps.current_user)`, and the structural
test is what keeps the class of mistake from coming back with the next route. (It
also needed a walk over `app.routes` rather than a filter: FastAPI 0.142 keeps an
included router as one lazy entry, so a flat `isinstance` check matches nothing and
would have reported every route as anonymous.)

### Phase 8 limitations

- **Inherited field block, now with a browser.** The dashboard can be shown a real
  stream and real visits, but the gate camera it can read plates from is still the
  rendered fixture: on the only real gate-style clip here the plates are 86 px
  against a 110 px floor. `gate_visit` therefore streams with `read_plates: false`
  and every frame drawn on it says `SYNTHETIC`. No API number in the table above is
  an ANPR accuracy claim.
- **Two replay cameras is the demonstrated ceiling on this GPU**, not a capacity
  claim. `gate_cam1` (the 8.5 k-frame road clip) is deliberately left out of
  `api.pipeline.cameras`: three YOLO contexts on a 6 GB card is a different
  measurement, and nothing here says what it would be.
- **`/health` requires a token**, of any role. It reports the GPU, the database and
  the camera inventory, so "an unauthenticated probe can enumerate the estate" is
  not a trade worth making for the convenience of a monitoring box; a health check
  needs an account, and that account can see nothing else.
- **The `fee` field in the doc's `session_completed` sample is not sent.** There is
  no tariff to compute it from. Same reason `/stats/revenue` is a 501.
- **Refresh tokens are not revoked.** There is no store for a revoked `jti`, no
  logout endpoint that would use one, and no session table for the web layer;
  deactivating an account *is* honoured on the next request (401 `user_inactive`),
  which covers the case that matters on a single-lot installation. A real
  deployment behind a load balancer will want the deny-list.
- **No HTTPS, no rate limiting, no lockout.** The doc lists TLS in the deployment
  phase; uvicorn serves plain HTTP on `127.0.0.1` here, and a login endpoint
  without a throttle is a brute-force target the moment it listens on an interface
  someone else can reach. Argon2's ~50 ms is a cost, not a defence.
- **CORS starts empty on purpose**, so no other origin can call this API at all and
  the failure is a console message on the first request rather than a door quietly
  open. Phase 9's dev server will need `allow_origins: ["http://localhost:5173"]`
  named in `configs/app.yaml`; the guard test refuses a wildcard.
- **`passes: 0` on a looping LOT camera** is a reporting wart the two-loop
  architecture produces (above). It is documented rather than smoothed over,
  because a monitoring rule that reads it as a stall is exactly the thing a future
  operator needs to be able to check.
- **The `--persist` server writes the same rows the CLI does**, from the same
  worker, with the same single-writer constraint Phase 7 stated. A second process
  writing one SQLite file is not supported; it is a Postgres question.

## Phase 9 - React + TypeScript + Vite dashboard

A responsive operator dashboard built to section F of the architecture document.
It consumes the Phase 8 API over REST for snapshots and WebSocket for live deltas,
drawing slot overlays from the stream data rather than polling.

**What it shows.** Login screen (Argon2id-hashed credentials), summary counters
that are derived from the slot list itself (`frontend/src/lib/slots.ts`) so that
`total = occupied + available + unknown` always holds - a bay the system has no
reading for gets its own "Not Reported" card instead of silently counting as free,
an SVG slot map colour-coded by status (green = FREE, red = OCCUPIED, grey =
UNKNOWN), a recent-sessions table with plate masking per role, and a 7-day
revenue bar chart via Recharts.

**Live updates.** The dashboard opens a WebSocket to `/ws/live` - the multiplexed
event channel - and applies `slot_occupied`, `slot_freed` and `slot_snapshot`
frames to the bay list, matched on `payload.slot_id` against the REST rows'
`slot_code`. `/ws/cameras/{id}/overlay` is a different socket: it carries per-frame
box geometry for the MJPEG picture and no bay statuses at all, which is why the
dashboard does not use it. The client reconnects automatically on disconnect.

**Bays start at UNKNOWN, and leave it on the first real reading.** `SlotRepository.sync`
creates a row with `current_status = UNKNOWN`, and the FSM only ever emits a
transition when a bay changes - so a bay the camera sees empty from the very first
frame would never have spoken, and would have stayed UNKNOWN forever. The
occupancy engine now publishes one `slot_snapshot` per bay after its first pass of
real evidence, reporting each bay's *committed* state (debounce folded away, so it
can never carry a `PENDING_*` value into a row). Persistence writes it exactly like
any other status change, which also closes an occupancy interval an earlier run left
open. Nothing maps UNKNOWN to AVAILABLE: a camera that is down still reports
UNKNOWN, and `mark_all()` keeps it that way.

**Stack.** React 19, TypeScript, Vite 8, TanStack Query (data fetching), Recharts
(charting), Lucide React (icons), Tailwind CSS v4 (styling). No state-management
library beyond React's own hooks — the doc calls this an MVP dashboard, not a
production SaaS.

**Tests.** `npm test` runs the vitest suite (`vitest run`, 17 tests in
`frontend/src/lib/slots.test.ts`): every status code classified into a counter
bucket (UNKNOWN and anything unrecognised land in "not reported", never in
"available"), the counts reconciling with the layout for the exact fixture that
prompted the fix (3 OCCUPIED + 2 UNKNOWN of 5), and `applySlotMessage` accepting
only the real `/ws/live` envelope - matched on `payload.slot_id` vs `slot_code` -
while ignoring other channels, unknown bays, malformed frames, and the fictional
`{type: "SLOT_UPDATE", slot_id: 3}` shape it used to expect. The backend half of the
same behaviour is `tests/unit/test_slot_status_init.py` (10 tests): the first frame
announces every bay once without counting as a transition, a mid-dwell bay is
announced in its committed state, a bay nobody looked at stays UNKNOWN, and the
announcement closes an interval an earlier run left open.

### Running the dashboard

```bash
# Terminal 1: start the backend (Phase 8)
cd "C:\Users\Saurabh\Downloads\Parking ai sysytem"
python scripts/migrate.py upgrade head        # ensure tables exist
python scripts/create_user.py --email admin@parking.local --role ADMIN --password 'Parking2026Secure!'
python scripts/serve.py                       # http://127.0.0.1:8000

# Terminal 2: start the frontend dev server
cd "C:\Users\Saurabh\Downloads\Parking ai sysytem\frontend"
npm run dev                                   # http://localhost:5173
```

Open http://localhost:5173 in a browser and log in with the admin account created
above. The dashboard will connect to the API at http://localhost:8000 automatically
(see `frontend/.env`).

### CORS configuration

The backend allows the Vite dev server origin in `configs/app.yaml`:

```yaml
api:
  cors:
    allow_origins: ["http://localhost:5173"]
```

Without this line, the browser blocks every fetch with a CORS error. The setting
is strict: only named origins can call the API, matching the doc's security
requirement.

### Limitations

- **Slot polygons are grid-generated.** The calibration tool (`scripts/calibrate.py`)
  produces polygon configs, but the dashboard currently renders a uniform grid
  because the slot geometry JSON format has not been wired into the SVG renderer.
  This is a visual limitation only — the status colours and counts come from the
  same WebSocket events.
- **No live video feed.** The MJPEG endpoint (`GET /cameras/{id}/stream.mjpg`)
  exists in Phase 8 but is not embedded in the dashboard yet. The slot map serves
  as the primary live view.
- **Revenue chart uses stub data.** The `/stats/revenue` endpoint returns 501
  (billing is out of scope per Phase 8 decisions), so the chart is hidden until
  real revenue data is available.
- **Single-camera view.** The dashboard hard-codes `lot_cam1` as the camera to
  subscribe to. Multi-camera selection is a Phase 10 feature.

## Phase 10 - Deployment scaffolding

Production deployment configuration in the `deploy/` directory, built to section H
of the architecture document. Provides one-command startup via Docker Compose with
a GPU-enabled FastAPI backend, the React frontend (built by a one-shot job, then
served by nginx), and an nginx reverse proxy handling TLS termination and rate
limiting. Verified on this machine (Docker Desktop + NVIDIA Container Toolkit) on
2026-10-08: all containers healthy, dashboard live at http://localhost, soak test
passed.

**What's included.** `docker-compose.yml` defining four services (api, nginx, the
one-shot frontend-build job, and the optional `db`), three Dockerfiles (API with
NVIDIA CUDA base, frontend build job, nginx reverse proxy), nginx configuration for
the reverse proxy, environment template (`.env.example`), secrets generation script
(`setup-secrets.sh`), database backup/restore scripts (`backup.sh`, `restore.sh`),
and a soak test script (`soak-test.sh`) that monitors health and records resource
usage.

**The database is SQLite, on purpose.** The recorded decision is "SQLite now,
PostgreSQL later": `SMART_PARKING_DATABASE_URL` (the one name both the running API
and `scripts/migrate.py` read) defaults to `sqlite:///data/smart_parking.db`, a file
in the host-mounted `data/` directory, so every record survives container rebuilds.
The compose `db` service is real but optional - start it with
`--profile postgres` and point that one variable at it when the switch happens. An
earlier compose file set a `DATABASE_URL` nothing read while the app wrote SQLite;
that dead variable is gone.

**The frontend is same-origin.** The built bundle calls its own origin; nginx
proxies `/api` and `/ws` (with the WebSocket upgrade) to the API, so CORS never
applies and `configs/app.yaml` ships an empty allow-list. `npm run dev` gets the
same shape from Vite's proxy. The frontend-build service writes the bundle straight
into `frontend/dist`, the directory nginx serves, so `up -d --build` publishes a
fresh build with no manual copy step.

**Running the deployment.** Full documentation is in `deploy/README.md`, covering:
prerequisites (Docker Desktop, NVIDIA Container Toolkit, 8 GB RAM), quick start
(generate secrets, configure environment, `docker compose up -d --build`), SSL/TLS
certificate setup (Let's Encrypt or self-signed), maintenance tasks (backup, restore,
update), troubleshooting, and a security checklist.

```bash
# Quick start (requires Docker + NVIDIA Container Toolkit)
cd "C:\Users\Saurabh\Downloads\Parking ai sysytem"
bash deploy/setup-secrets.sh                  # generate db_password.txt, jwt_secret.txt
cp deploy/.env.example deploy/.env            # then edit the values it names
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec api python scripts/create_user.py \
    --email admin@parking.local --role ADMIN --password 'YourSecurePassword123'
# Dashboard: http://localhost  |  API docs: http://localhost/api/v1/docs
```

On Windows the Docker CLI lives at
`C:\Users\Saurabh\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`
and is not on PATH; run the commands from Git Bash after Docker Desktop is up
(solid whale icon in the tray), or use the full path.

**Soak test.** The stability test runs continuous health checks and monitors
memory usage. Pass criteria: zero failures over the window.

```bash
bash deploy/soak-test.sh 24                   # 24-hour test
bash deploy/soak-test.sh 0.2                  # ~12-minute smoke test (passed 2026-10-08)
```

**Limitations.** The API image's dependency layer needs a network to rebuild
(torch, ultralytics), which this machine does not always have; the compose file
therefore mounts `src/` read-only and puts it on `PYTHONPATH`, so a code change
deploys with a container restart instead of an image rebuild. TLS is configured but
not enabled by default (the 443 server block in `deploy/nginx.conf` is commented
out), and the field blockers from Phases 5-6 still stand: no entrance-camera
recording here reaches the 110 px readable plate floor.

## Verified results (Phase 1-8 acceptance)

| Check | Target | Measured |
| --- | --- | --- |
| Detection throughput on 1080p-class clip | >= 15 FPS | **17-53 FPS** over repeated runs (yolo11s, imgsz 640, FP16, CUDA). The spread is the laptop GPU idling at 600 MHz between runs, not the pipeline; the `mean_fps` the scripts print is lower still because it includes first-call model warmup |
| Detection quality | few obvious misses | 6/6 vehicles boxed correctly on the lot clip, conf 0.60-0.92 |
| Slot accuracy | >= 95% on labelled frames | parked cars score containment 1.00 in B1/B2/B3, 0.85-0.93 in A1/A2; aisle cars score 0.00 everywhere |
| Flicker | none over a clip | zero spurious transitions on a continuous shot; every slot history is monotonic |
| Replay coverage | every frame evaluated | 148/148 frames of `lot_shot5.mp4`; `--stride 3` reproduces the same 3 occupied slots as stride 1 |
| Velocity gating | a moving vehicle never claims a bay | `lot_shot5.mp4`: 1 false bay claim before gating, 0 after, with the three genuinely parked cars (A2/B1/B2) unchanged. See the Phase 4 section |
| ANPR on a real gate camera | plate reads from footage | **not achieved.** The only gate-style clip here tops out at an 86 px plate where the readable floor is 110 px, so the pipeline correctly returns 0 candidates from 138 vehicles. Field OCR accuracy is unmeasured, not poor - it needs an entrance-camera recording |
| ANPR software chain | every stage testable end to end | 90 ANPR tests pass on top of the 57 from Phases 1-4; the labelled fixture replay turns 24 reads into 1 correct stable plate (`MH20AB1234`) with 1 event and 0 false plates |
| Gate sessions on a real barrier | ENTER at t1, EXIT at t2, duration | **not achieved - no gate recording exists here.** On real footage the stage runs (450 frames, 7 crossings, 6 sessions) but every plate column is empty, because that clip's plates are 86 px against a 110 px floor. Same field blocker as Phase 5 |
| Gate session correctness | no duplicate events; durations within a few seconds of ground truth | **5/5 on the labelled synthetic clip**: every entry/exit landed on its expected frame (16/60, 21, 15/43, 76, none), durations 2.93 s and 1.87 s against a designed ~2.9 s and dwell, 0 duplicate events, 1 honest ANOMALY for the exit-without-entry, 5/5 plates read correctly - all labelled `SYNTHETIC FIXTURE` |
| Gate + session test coverage | no existing test weakened | **234 tests pass** (57 from Phases 1-4, 90 ANPR, 87 gate) at the end of Phase 6, and all 234 still pass in Phase 7's **309**, none edited, weakened or skipped |
| Gate stage cost | not the bottleneck | 3560 frames/s steady for crossing logic + sessions on a 90-frame replay (~34 ms end to end); the OCR stage is what limits a plate-reading gate run to 1.9 frames/s |
| Schema: models vs migrations | the same schema, not two descriptions | `alembic check` on a migrated file database reports **no new upgrade operations**, and `test_the_migrated_schema_matches_the_models_exactly` diffs every column, index, foreign key and check constraint; `downgrade base` removes every table again (seven at Phase 7, all **nine** since `0002`, and `test_the_migrations_are_reversible` round-trips the nine) |
| Phase 7 end-to-end | a run's visit reads back unchanged | **29 persistence tests**, including the required chain test: a visit on the Phase 6 bus is written by the service, read back from SQLite and compared field by field (`source_session_id`, status, entry/exit times, 80.0 s duration, normalised plate, OCR confidence, vehicle class, and the three log rows attached to it) |
| Duplicate and anomaly handling in the database | duplicate entries do not make duplicate sessions | replaying one visit twice leaves **1 session row** and 6 log rows; a second live claim for one plate yields `[ACTIVE, ANOMALY]` sharing one plate row without tripping the partial unique index; an exit whose entry was never persisted keeps `entry_time NULL` and `duration NULL` |
| Transactions and rollback | failures roll back, nothing half-written | a batch containing an unknown camera raises and leaves **0 sessions and 0 log rows**; the same poison event inside a worker run costs only itself (1 failure recorded, the good visit still written, the error surfaced by the CLI) |
| Row volume | never one row per frame | 148-frame lot replay: 151 events, 3 bay updates + 3 stay intervals + 16 log rows; the 135 per-frame SUMMARY events wrote nothing. Batched writes measured at 600 events/s against 226/s committing per event |
| AI layers keep their independence | no SQLAlchemy in a detector | guard test scans `core`, `detection`, `parking`, `tracking`, `anpr`, `pipeline`, `video` and `services/session_service.py`: **no import of `smart_parking.db`, `sqlalchemy` or `alembic`** |
| Phase 7 test coverage | 234 existing tests intact | **309 tests pass** (234 from Phases 1-6 + 75 added: 20 schema/migration, 25 repository, 29 persistence, 1 session-timestamp guard) |
| Schema after Phase 8 | still one description of the database | `alembic check` reports **no new upgrade operations** with the nine tables, `test_every_orm_table_is_in_the_list_health_checks` keeps `MIGRATED_TABLES` equal to the metadata, and the two new tables carry their own constraints: email unique, role an enum the token layer knows, `action IN (the eleven the code writes)`, and an account with audit history that cannot be deleted |
| Auth on real cost parameters | a KDF, not a hash | Argon2id **50-65 ms to hash, ~50 ms to verify** at the shipped 64 MiB/3/4; login 85-124 ms end to end; one 401 answer for unknown-email / wrong-password / deactivated, with the reason kept only in the audit row |
| Token separation | a refresh token cannot call an endpoint | refresh with an access token is 401 `token_wrong_use`; an expired token, a foreign signature, a missing `use` and a non-member `sub` each get their own code; a role that changed since minting is 403 `role_changed`, and a deactivated one is 401 on the next request |
| RBAC | four roles, one table of rules | every route × every role asserted in `test_api_endpoints.py`: reads for all, `ADMIN+OPERATOR` for operator writes, `ADMIN` for accounts/cameras/slots, `ADMIN` alone for `/audit-logs`; `/health` needs a token because it reports GPU + database + camera inventory |
| Plate masking | full plate only where a person is accountable | `TS09DP4567 -> TS09**4567` for `VIEWER`/`CUSTOMER` everywhere and for every role in `/plates/search`; `ADMIN`/`OPERATOR` see full plates only on a session or an overlay; `plate_corrected` stores both sides masked |
| Live stream | a picture that does not slow the pipeline | gate clip **14.8 fps** (its own 15 fps source), lot clip **29.7 fps** (its own 30 fps source) with real YOLO on the GPU, snapshot/overlay **5-8 ms**, `/stream.mjpg` **16 JPEGs in 3.1 s** at the configured 5/s, `/ws/live` **606 events in 20 s with `dropped = 0`** |
| Inference never blocks on a browser | bounded per-viewer queue | a filled `ws_queue_size` drops the unread event and counts it (`hub.dropped`), reported by `/health` as `dropped_events`; the bus callback runs on the inference thread and hands off with `call_soon_threadsafe` |
| Persistence and HTTP on one process | no lost tail | `worker.stop()` joins and drains again: the visit published between the stop button and the thread's last breath is in the database after shutdown (**exactly +1 session row** over the seeded control, `COMPLETED`, 80.0 s). The test that proves it found `app.py`'s `bus or InMemoryEventBus()`, which discarded a caller's *empty* bus, because `InMemoryEventBus.__len__` makes one falsy |
| A server running for a shift | bounded memory, honest count | failure sample capped at 200 verbatim entries, `failure_total` uncapped and what `stats`/both CLI summaries report; the 17-minute accepted run wrote **0 failures** and its log is **42 KB**, where the same server unpaced wrote 793 KB in 65 s |
| Row volume under a server | still never one row per frame | 17 minutes of two looping replay cameras, 21 694 lot frames + 10 769 gate frames: **4 sessions, 8 detection logs, 3 occupancy intervals, 16 audit entries** |
| Phase 8 test coverage | 309 existing tests intact | **542 tests pass** (309 from Phases 1-7 + 233: 228 in the four `test_api_*` files, 4 schema, 1 persistence), none of the 309 edited, weakened or skipped; both CLIs re-run and reproduce their Phase 7 records exactly |
| Every route takes a token | only the two that issue one | 29 REST routes walked from the mounted app's dependency graph: the anonymous set is exactly `/auth/login` and `/auth/refresh`. Found and fixed one route that did not follow the rule (`/stats/revenue` answered 501 unauthenticated) |

## Known limitations

- **The bundled demo clip is an edited compilation** with hard cuts at frames
  254/359/440/574. Across a cut the calibration is invalid and slots flicker -
  that is a footage problem, not a pipeline bug, and it is why `configs/cameras.yaml`
  points at trimmed single-shot clips. Replace with one continuous fixed-camera
  recording of a real lot for the report.
- **The velocity threshold is per-camera.** It is measured in pixels/second at
  the source resolution, so 30 px/s means something different at 478x850 than at
  1080p, and it says nothing about real-world speed. Re-measure with
  `scripts/measure_velocity.py` after any change of camera, mounting position or
  resolution. See the Phase 4 limitations above.
- **ANPR is not field-validated.** The plate stage is built, measured on a
  rendered fixture and unit-tested, but no footage on this machine has a plate
  wide enough to read, so nothing about its real-world accuracy is known. Run
  `scripts/measure_plates.py --camera <gate>` on any candidate recording before
  believing an ANPR number. See the Phase 5 limitations.
- **Gate sessions are not field-validated either.** The state machine is proven
  against a labelled synthetic clip frame-for-frame, but the only gate-style
  recording here is a two-way road whose plates are unreadable, so neither the
  entry/exit counts nor the durations have been measured on a real barrier.
  Calibrate a real clip with `scripts/calibrate.py --gate` and re-run
  `scripts/run_gate.py --camera <gate>`. See the Phase 6 limitations.
- **The database inherits that block, it does not fix it.** Persistence is proven
  end to end (event -> service -> repository -> SQLite -> read back) and on real
  footage, but on the real footage every session row has `plate_id` NULL, so a
  database of identified visits still needs the same entrance-camera recording. The
  plate-bearing numbers in the Phase 7 tables come from the rendered fixture clip
  and are labelled as such. See the Phase 7 limitations.
- **The API inherits it a third time, and says so on screen.** Every endpoint,
  token, socket and stream in the Phase 8 section works - but the gate camera it
  can read plates from is the rendered fixture, whose frames are drawn with
  `SYNTHETIC` on them, and it is served with `read_plates: false`. A browser
  looking at this system sees a real lot stream and synthetic visits. See the
  Phase 8 limitations.
- **HTTP here is local, plain and unthrottled.** `serve.py` binds `127.0.0.1` with
  no TLS, no rate limit and no failed-login lockout, and CORS allows no other
  origin at all. That is safe for a laptop demo and not safe for an interface
  someone else can route to; the doc's TLS line belongs to Phase 10.
- **Merged detections.** A car partly hidden by the foreground bush sometimes
  yields one oversized box spanning two bays, which reads as a straddle
  violation. BoT-SORT with camera-motion compensation is the planned mitigation.
- `shapely` is installed but unused so far; the clipping in `geometry.py` is
  exact for rectangle-vs-convex-polygon and avoids the dependency at runtime.

## Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Video sources + YOLO vehicle detection | done |
| 2 | Slot calibration + geometry | done |
| 3 | Occupancy FSM + debounce + counters | done |
| 4 | ByteTrack gating, stationary tracks feeding occupancy | done |
| 5 | ANPR: plate detector + OCR + validator + multi-frame voting | built and unit-tested; field OCR blocked on footage (no plate reaches the readable width) |
| 6 | Gate lines, entry/exit sessions, track-to-plate binding | built and unit-tested; session correctness proven frame-for-frame on a labelled synthetic clip, field durations blocked on the same missing footage as Phase 5 |
| 7 | SQLAlchemy models + Alembic (SQLite now, PostgreSQL later) | built, migrated and tested: seven tables, the doc's ACTIVE-session uniqueness enforced by partial unique indexes, 75 tests, `--persist` on both CLIs; identified visits still wait on the same footage as Phases 5 and 6 |
| 8 | FastAPI REST + JWT/RBAC + WebSocket hub + MJPEG | built, served and tested: 29 REST routes + 2 sockets over the doc's four roles, Argon2id + HS256 with a `use` claim, plate masking, `users` + `audit_logs` in `0002`, camera threads started from the lifespan and paced to each clip's own frame rate, 233 tests; the billing routes are refused rather than faked, and real plate reads still wait on the same footage as Phases 5-7 |
| 9 | React + TypeScript + Vite dashboard | built and running locally at http://localhost:5173; login, slot map, session table working; revenue chart stubbed (billing out of scope); slot polygons grid-generated pending calibration geometry wiring |
| 10 | Docker Compose, HTTPS, soak test | scaffolded with full deployment docs in deploy/README.md; docker-compose.yml, 3 Dockerfiles, nginx configs, backup/restore scripts, 24h soak test script; untested on this machine (Docker not installed) |

Docker is not installed on this machine, so Phase 7 runs on SQLite through
SQLAlchemy with PostgreSQL-compatible types and constraints. The partial unique
indexes the doc draws as a Postgres feature are not deferred: SQLite has had them
since 3.8, so "one ACTIVE session per camera+track / plate / slot" is a database
rule here and is tested as one. Moving to Postgres is `database.url`, an
`asyncpg`-style driver and nothing in `db/` that reads YAML changing.
