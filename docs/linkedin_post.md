# LinkedIn post and article — AI-Based Smart Parking Management System

Every figure below is taken from `README.md`'s measured-results table, `docs/project_report.md`, or test
runs executed on this machine. Nothing is estimated.

---

## 1. LinkedIn post (fits the 3,000-character limit)

```text
Can one ordinary camera replace a person walking around a car park counting free spaces?

I built and verified an AI-Based Smart Parking Management System that answers occupancy from overhead video: detection, tracking, occupancy logic, entry/exit sessions, database, API, and a real-time dashboard. All processing runs on-device (RTX 4050).

THE PIPELINE
- OpenCV frame read -> YOLO11s vehicle detection (FP16 on CUDA; measured 17-53 FPS on 1080p-class video)
- ByteTrack assigns stable IDs, so one parked car is never counted as five
- A velocity gate rejects tracks that are merely driving past (measured: 1 false bay claim before gating, 0 after)
- Vehicle footprint vs hand-calibrated bay polygon: containment score decides the bay (parked cars up to 1.00, aisle cars 0.00)
- Debounced state machine: 0.6 s of evidence to mark a bay OCCUPIED, 8 s of silence to free it. Measured flip: exactly 0.600 s. Zero spurious transitions across a full clip
- A camera that stops reporting yields UNKNOWN, never a false FREE

ENTRANCE CAMERA (separate pipeline, in parallel)
- Plate detection -> preprocessing -> OCR -> Indian registration-number validation -> multi-frame voting -> gate line crossing -> parking session with entry/exit timestamps and duration
- Proven frame-for-frame on a labelled synthetic clip: 5/5 vehicles, every entry/exit on its expected frame, durations 2.93 s and 1.87 s against designed values, 0 duplicate events, 1 honest ANOMALY (exit without entry)
- Honest gap: NOT field-validated. No recording I have reaches the ~110 px plate width the OCR needs (my only gate clip tops out at 86 px), so I quote no real-world ANPR accuracy

BACKEND
FastAPI: 29 REST routes + 2 WebSocket channels, 4 roles, Argon2id password hashing, JWT access/refresh with server-side revocation, one-time WebSocket tickets, role-based plate masking, failed-login throttling.

DATA
SQLAlchemy + Alembic (3 migrations, 9 tables) on SQLite, schema written Postgres-compatible. Events flow through one in-memory bus to two subscribers: a persistence worker thread and a WebSocket hub.

FRONTEND
React + TypeScript + Vite operator dashboard: live bay map drawn from real calibration polygons, counters that always reconcile (total = occupied + available + not-reported), the real annotated MJPEG stream, session history, occupancy analytics, system health.

VERIFIED (not claimed - measured)
- 646 backend tests + 105 frontend tests pass
- 148/148 frames of the test clip evaluated; every bay history monotonic
- Streams served at each clip's own frame rate (29.7 fps lot, 14.8 fps gate); MJPEG 16 frames in 3.1 s; 0 dropped WebSocket events

PLANNED (not built)
BoT-SORT motion compensation for merged detections, re-calibration for the real mounting position, and entrance-camera footage to validate ANPR properly.

Code: https://github.com/saurabh876724/Smart-parking

#ComputerVision #YOLO #Python #FastAPI #React #OpenCV #MachineLearning
```

---

## 2. LinkedIn article (no length limit — use "Write article" on LinkedIn)

**Title:** How I Built an AI Parking Occupancy System That Runs on One Camera

**Subtitle:** YOLO, ByteTrack, a debounced state machine, and a FastAPI/React stack - what I built, what I
measured, and what I still cannot prove.

### The problem

An attended car park cannot tell you, without sending someone to walk the rows, how many bays are free and
which ones. Installing a sensor in every bay is expensive; a paper register is slow. This project turns one
ordinary overhead camera per row of bays into that answer, and pushes it to a dashboard within a second of a
car stopping.

### What the system does

It watches video, detects vehicles, keeps identity across frames, decides which bay each vehicle occupies,
converts that into a debounced occupied/free state, and publishes the result as events that reach a database
and a browser. A second, independent pipeline on an entrance camera reads number plates and turns gate
line-crossings into parking sessions with durations.

### The perception pipeline, stage by stage

1. **Frame read.** OpenCV reads the source. Live cameras get a threaded grabber that keeps the newest frame;
   recorded replay reads frames inline and drives its timers from the video's own clock, so replaying a clip
   3x faster does not look like cars leaving 3x faster.
2. **Detection.** YOLO11s, FP16 on CUDA, filtered to the four vehicle classes, confidence >= 0.35. Measured
   17-53 FPS on a 1080p-class clip on a laptop RTX 4050 (the spread is GPU clock throttling, not the pipeline).
3. **Tracking.** ByteTrack assigns each detection a persistent ID. Verified: the same five IDs across samples
   2 seconds apart while boxes moved hundreds of pixels.
4. **Velocity gating.** A track whose median speed exceeds 30 px/s is driving, not parking. Measured on the test
   clip: one false bay claim before the gate, zero after - and the three genuinely parked cars unchanged.
5. **Bay assignment.** The vehicle's ground footprint polygon (the bottom 40% of its box) is tested against each
   calibrated bay polygon. The score is containment - how much of the *vehicle* is inside the bay - not how much
   of the bay is covered, because a big bay with a small car parked perfectly in it scores 0.19 under the
   conventional overlap ratio, below any sensible threshold. Assignment is greedy, so one car cannot claim two bays.
6. **Occupancy state machine.** FREE -> PENDING_OCCUPIED -> OCCUPIED after 0.6 s of continuous evidence;
   OCCUPIED -> PENDING_FREE -> FREE after 8 s of silence. The asymmetry is deliberate: a wrong OCCUPIED corrects
   itself in a second, a wrong FREE double-books a bay. The 8-second timer doubles as occlusion tolerance.
   PENDING states never leave memory - the database and the API only ever carry committed states.
7. **Events out.** One in-memory event bus carries every transition.
8. **Subscribers.** A persistence worker thread drains the bus in batches and writes rows; a WebSocket hub pushes
   the same events to every connected browser, with a bounded per-viewer queue so a slow client drops its own
   messages instead of stalling inference.
9. **Dashboard.** The browser seeds its bay list from REST, then stays current from the socket. Counters are
   derived from the bay list itself, so the cards and the map can never disagree.

### The entrance camera: plates and sessions

A vehicle track is cropped to a plate candidate, preprocessed, run through OCR, validated against Indian
registration-number structure, and voted across multiple frames before a plate is accepted. Independently, the
track's crossing of a calibrated gate line produces entry/exit events, and the two are bound together into a
parking session with a duration.

The session logic is proven frame-for-frame against a labelled synthetic clip: 5/5 vehicles, every entry and
exit on its expected frame, durations of 2.93 s and 1.87 s against the designed values, zero duplicate events,
and one ANOMALY correctly reported for an exit with no matching entry.

What I cannot claim: real-world ANPR accuracy. Plates are only readable at roughly 100+ px in width, and no
recording I have reaches that (my only gate-style clip tops out at 86 px). The pipeline correctly returns zero
candidates there instead of inventing reads - so the honest statement is "built and unit-tested, field
validation pending footage".

### Backend, data, and security

FastAPI serves 29 REST routes and two WebSocket channels. Authentication is Argon2id (measured 50-65 ms per
hash at the shipped cost parameters) with a 15-minute access token and a 12-hour refresh token that the server
can revoke. WebSockets authenticate with one-time 15-second tickets rather than long-lived tokens in the URL.
Four roles; plate text is masked unless the caller's role is accountable for it. Failed logins are throttled.
Requests and logins land in an audit table.

Persistence is SQLAlchemy 2 + Alembic over nine tables, SQLite with WAL (Postgres-compatible types and
constraints, so the switch is a connection string). Migrations are reversible and a test asserts the migrated
schema matches the ORM models exactly.

### The dashboard

React 19 + TypeScript + Vite, dark operator UI: live bay map drawn from the real calibration polygons, occupancy
counters that always reconcile, the annotated MJPEG stream with boxes and track IDs, session history with
status filters, hourly occupancy analytics from persisted history, and a system-health view that distinguishes
the backend's state from this browser's connection state.

### How it was verified

- 646 backend tests and 105 frontend tests pass; none of the earlier phases' tests were edited, weakened, or
  skipped to make later phases green.
- A bay's debounce was measured from the event stream: FREE at frame-clock 904.101, OCCUPIED at 904.701 -
  exactly the 0.600 s the configuration promises.
- 148/148 frames of the test clip evaluated; slot histories monotonic (no flicker).
- Live streaming measured at each clip's own frame rate with zero dropped WebSocket events across a 20-second
  sample.
- Annotated frames come from the same drawing code the pipeline renders, so the overlay in the dashboard is what
  the pipeline actually concluded, not a redraw.

### Limitations, stated plainly

- ANPR and gate sessions are synthetic-fixture-verified, not field-verified (footage resolution).
- The velocity threshold is per-camera, measured in pixels per second at the source resolution.
- The demo clip is an edited compilation with hard cuts; the shipped configuration uses trimmed single-shot
  clips instead.
- Billing/revenue is deliberately not implemented - the API returns 501 rather than a fake chart.
- Partly hidden vehicles can merge into one oversized box and read as a straddling violation; BoT-SORT with
  motion compensation is the planned mitigation.

### What is next

Entrance-camera footage with readable plates to validate ANPR end to end; a re-calibration pass for a real
mounting position; stronger coupling between bay occupancy and gate sessions (today they are independent
measurements); and operational hardening - retention policy for history tables, and a longer soak run.

### Links

- Repository: https://github.com/saurabh876724/Smart-parking
- Architecture and working-flow diagrams, verified project report, and demo script: `docs/` in the repository
