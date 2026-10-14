# Smart Parking System — Complete Project Report

**Project:** AI-Based Smart Parking Management System (`smart-parking` v0.1.0)
**Repository:** `C:\Users\Saurabh\Downloads\Parking ai sysytem`
**Report date:** 14 October 2026
**Verification window:** 14 October 2026, 23:10 – 23:35 (local machine time)
**Method:** read-only inspection of the running system, live probing of the AI pipeline through an isolated scratch instance that wrote to no real database, and re-execution of the whole test suite.

> **Honesty rule used throughout this report.** Every number below is either a command output taken today, or a line of source code quoted by file and line number. Anything not verified is labelled *not verified* or *planned*, and nothing that is configured-off is described as working.

**Companion documents**

| Document | Contents |
|---|---|
| [`architecture.md`](architecture.md) | Section 3 — system architecture diagram (Mermaid) + component-by-component explanation |
| [`working_flow.md`](working_flow.md) | Section 4 — working flow diagram (Mermaid) + decision branches |
| [`viva_and_demo.md`](viva_and_demo.md) | Section 9 items 5–8 — viva explanation, 5-minute demo script, viva Q&A, limitations and future work |
| [`evidence/`](evidence) | Raw WebSocket event captures used as proof in Section 7 |
| [`figures/lot_cam1_annotated_frame.jpg`](figures/lot_cam1_annotated_frame.jpg) | One real processed frame (478×850) taken from the running MJPEG stream |

---

## Table of contents

1. [Current project status — verified](#1-current-project-status--verified)
2. [What the project is and how it works](#2-what-the-project-is-and-how-it-works)
3. [System architecture diagram](#3-system-architecture-diagram) → *separate file*
4. [Working flow diagram](#4-working-flow-diagram) → *separate file*
5. [Feature status table](#5-feature-status-table)
6. [Technology stack](#6-technology-stack)
7. [Testing and verification results](#7-testing-and-verification-results)
8. [How to run and demonstrate the project](#8-how-to-run-and-demonstrate-the-project)
9. [Presentation-ready deliverables](#9-presentation-ready-deliverables)
10. [One defect found during this audit](#10-one-defect-found-during-this-audit)

---

## 1. Current project status — verified

### 1.1 Verdict summary

| # | Question | Answer | How it was verified today |
|---|---|---|---|
| 1 | Is the frontend running? | **Yes** | Vite dev server listening on `[::1]:5173` (PID 21584); `curl http://localhost:5173/` → `HTTP 200`, 615-byte HTML shell |
| 2 | Is the backend healthy? | **Yes, running and answering correctly** | `python scripts/serve.py --persist` listening on `127.0.0.1:8000` (PID 17348). Unauthenticated `GET /api/v1/health` → `401 {"error":{"code":"token_missing",…}}`, which is the designed behaviour. *The health body of your own server was not read by this audit, because that needs your admin login.* Full `/health` output from the identical scratch instance is in §7.4 |
| 3 | Is the SQLite database healthy and readable? | **Yes** | Read-only connection to `data/smart_parking.db`: `PRAGMA integrity_check` → `ok`; `PRAGMA foreign_key_check` → 0 rows; `journal_mode` → `wal`; `alembic_version` → `0003` |
| 4 | Has the corruption issue been resolved? | **Yes — resolved and still resolved** | The recovered file is the one in use (569,344 B main + 4,120,032 B WAL). It has *grown normally* since recovery (190 detection rows, 27 occupancy-history rows, 4 sessions), so it is being written to successfully, not merely surviving |
| 5 | Is the AI video pipeline running? | **Yes** | Two camera threads (`lot_cam1`, `gate_visit`) per `configs/app.yaml → api.pipeline.enabled`; scratch instance processed **1,412 frames in 89 s of uptime** and `frames_in = 2744` at the same moment |
| 6 | Is YOLO actually detecting vehicles? | **Yes — on real footage** | `lot_cam1` source is `data/samples/lot_shot5.mp4` (real video, health reports `synthetic_replay: false`), and `/overlay` returned five vehicle tracks with confidences 0.46–0.94 and pixel bounding boxes that moved between samples (§7.3) |
| 7 | Are slot statuses updated from real video frames? | **Yes** | Live FSM transitions observed on the real clip: `A2 → OCCUPIED (track=4)`, `B2 → OCCUPIED (track=3)`, `B1 → OCCUPIED (track=5)` in the pipeline log, and the debounce was measured exactly: bay `A2` published `FREE` at frame-clock `…904.101` and `OCCUPIED` at `…904.701` — **0.600 s**, equal to `occupancy.occupied_after_s: 0.6` (§7.5) |
| 8 | Do WebSocket updates reach the dashboard? | **Yes, at the protocol level (verified)** | `WS /ws/live?ticket=…` handshake succeeded; **845 messages in 45 s**: 1 `hello`, 834 `summary`, 5 `slot_snapshot`, 3 `slot_occupied`, 1 `vehicle_exited`, 1 `violation`; `dropped_events: 0`, `ws_ticket_refusals: 0` (§7.5) |
| 9 | Are all required Docker services running? | **No** | Only `smart-parking-db` (Postgres 15) is `Up (healthy)`. The `api`, `frontend-build` and `nginx` services declared in `deploy/docker-compose.yml` are **not created** — their images exist, no containers do. Note also that the running Postgres is *unused*: `configs/app.yaml → database.url` is `sqlite:///data/smart_parking.db` |

### 1.2 What works, what is partial, what is absent

**Working and verified today**
- Sign-in, JWT access/refresh pair, role checks, one-time WebSocket tickets, login throttling, Argon2id password hashing.
- Vehicle detection (YOLO11s on CUDA), ByteTrack re-identification, slot-containment occupancy scoring, the slot FSM with debounce, the in-memory event bus, the persistence worker, all REST reads, the MJPEG picture, the live event channel, the React dashboard with its slot map, counters, session table and camera preview.
- Database persistence: 190 `detection_logs` rows, 27 `slot_occupancy_history` rows, 10 `parking_slots` rows, 4 `parking_sessions` rows, 2047 `audit_logs` rows.

**Partially working / by design incomplete**
- **Only 2 of the 4 configured cameras are streamed.** `gate_cam1` and `lot_cam1_shot1` are enabled in the *camera table* but absent from `api.pipeline.enabled.cameras`, so they never produce a frame. Their five `lot_cam1_shot1` bay rows honestly remain `UNKNOWN`, which the dashboard reports in a separate "Not Reported" card rather than calling them free.
- **ANPR is implemented but switched off on every served stream** (`read_plates: false`), so `license_plates` and `vehicles` tables have **0 rows** and plate search returns an empty page.
- **Session durations are empty** in the replay data: `GET /stats/duration` → `{"average_s": null, "sample_size": 0, "arrivals_considered": 0, "peak_hours": []}`.
- **Replay only, and the loop suppresses repeats.** `api.pipeline.first_pass_events_only: true` means a looping clip keeps streaming frames but stops re-emitting the same events; after the first pass the socket carries `summary` only.

**Not implemented**
- **Billing / revenue**: `GET /api/v1/stats/revenue` → `HTTP 501 {"error":{"code":"not_implemented","message":"revenue reporting needs tariffs and payments, which are a later phase"}}` (`src/smart_parking/api/routers/stats.py:93`). The dashboard hides that section rather than showing a fake chart (`frontend/src/components/Dashboard.tsx:44-54`).
- **Physical camera integration**: the code path exists (`video/sources.py:39 open_capture` treats a digit as a webcam index and a URL as an RTSP/HTTP source via `CAP_FFMPEG`) but **no camera source is configured or connected**; both served streams are files on disk.
- **A deployed (Docker) stack**: compose file, Dockerfiles and nginx site are all written and validated, but the services are not currently running.

### 1.3 Live database — state read today (read-only)

```
data/smart_parking.db        569,344 B      (main file)
data/smart_parking.db-wal  4,120,032 B      (WAL, expected with journal_mode=wal)
data/smart_parking.db-shm     32,768 B

PRAGMA integrity_check       -> [('ok',)]
PRAGMA foreign_key_check     -> 0 violating rows
PRAGMA journal_mode          -> ('wal',)
alembic_version              -> ('0003',)

cameras 4 | parking_slots 10 | parking_sessions 4 | slot_occupancy_history 27
detection_logs 190 | license_plates 0 | vehicles 0 | users 2 | audit_logs 2047

parking_slots
  lot_cam1         A1 FREE      A2 OCCUPIED  B1 OCCUPIED  B2 OCCUPIED  B3 FREE
  lot_cam1_shot1   A1 UNKNOWN   A2 UNKNOWN   B1 UNKNOWN   B2 UNKNOWN   B3 UNKNOWN
```

The five `UNKNOWN` rows are **correct behaviour**, not damage: that camera has no pipeline, so nothing tells the system the bays are empty, and `UNKNOWN` is never auto-treated as `FREE`.

### 1.4 The corruption incident — closed, with one standing rule

On 13 October 2026 the live database was found corrupt. It was recovered into a separate file (originals preserved under `data/incident_20261013/original/` and `…/damaged_in_place/`), the recovery was promoted, and `scripts/serve.py` now refuses to start a second writer against the same SQLite file (verified today: a competing start exits with code 1 and the message *"Two writers on one SQLite file is not a slow path; it is a corrupted database"*).

**Standing rule:** never run `scripts/serve.py` on the host **and** the `api` Docker container against `data/smart_parking.db` at the same time, and never start a second host server on the same database. One writer only.

---

## 2. What the project is and how it works

### 2.1 The problem

A attended car park cannot answer, without a person walking around, "how many bays are free right now, and which ones?". Paper registers and sensor-per-bay installations are either slow or expensive. This project turns **one ordinary overhead camera per row of bays** into that answer, automatically: it watches the video, finds the vehicles, decides which bay each vehicle sits in, and shows the result on a dashboard that updates within a second of the car arriving.

### 2.2 Objectives

| Objective | Where it is met (verified) |
|---|---|
| Detect vehicles from video without per-bay hardware | `detection/vehicle_detector.py:78 VehicleDetector`, COCO classes `[2,3,5,7]` |
| Follow the same vehicle across frames so a parked car is not counted five times | `tracking/tracker.py:57` ByteTrack call; persistent ids observed over 3 samples |
| Decide occupied/free geometrically, from hand-drawn bay polygons | `configs/slots/lot_a_cam1.json`, `parking/geometry.py:160 containment_score` |
| Never flicker a status the camera is unsure about | `parking/slot_fsm.py` debounce (0.6 s to occupy, 8.0 s to free) |
| Distinguish "empty" from "not being told" | `SlotStatus.UNKNOWN`, `mark_all` on offline, `Not Reported` card |
| Persist history for reports | `services/persistence.py:407` batched writes; 27 history rows today |
| Push updates to an operator's browser | `api/live.py:199 WebSocketHub` → `WS /ws/live` |
| Control who sees what, and log it | `api/deps.py`, `api/security.py`, `audit_logs` (2047 rows) |

### 2.3 Key features

Real-time bay map (polygons drawn from the calibration file), live counters (total / occupied / available / not-reported), annotated camera picture with bounding boxes, tracking ids, bay polygons and a TOTAL/OCCUPIED/FREE HUD burned into the JPEG, active-session counting from the server's own filter, recent-session table, hourly occupancy series from persisted history, plate search, audit log, MJPEG preview inside the dashboard, live event socket with an honest connection chip (`Live` / `Reconnecting` / `Not live`), and role-gated configuration writes.

### 2.4 How an administrator uses the application

1. Opens the dashboard URL and signs in (`LoginPage.tsx`); the API returns an **access token (15 min)** and a **refresh token (12 h)**.
2. Sees the counters and bay map for `lot_cam1`: the map's polygons are the calibrated geometry, each bay's colour is its committed status, and a bay the camera cannot report is drawn in the `UNKNOWN` style, not as "free".
3. Watches the camera card: the picture is the same annotated frame the detector worked on, so the boxes over a bay explain why that bay is marked occupied.
4. Sees the header chip read **Live** — that is the `/ws/live` channel, whose state is tracked separately from the page loading.
5. Signs a vehicle in/out only through the pipeline (entry/exit events), and can search a plate (`/plates/search`, ≥3 normalised characters) or review the persisted session history.
6. Admin-only actions: add or re-draw a bay polygon (`POST /slots`, `POST /slots/{id}/polygon`) and manage accounts. Both are written to `audit_logs`.
7. After 15 minutes the access token expires; the API layer silently refreshes once and, if the refresh is refused or withdrawn server-side, returns to the sign-in screen **with the reason** for the sign-out.

### 2.5 How a vehicle is detected

```
frame → OpenCV read → YOLO11s inference (conf ≥ 0.35, imgsz 640, CUDA + FP16)
      → keep only COCO ids 2/3/5/7 (car, motorcycle, bus, truck)
      → ByteTrack: each detection gets (or keeps) an integer track id
      → velocity gate: a track moving > 30 px/s median is "driving", not "parking"
      → footprint polygon (bottom 40 % of the box) tested against every bay polygon
```

`detect()` returns bounding boxes and confidences; the tracker's `persist=True` is what makes id `211` still be id `211` two seconds later even though its box moved 80 pixels. The velocity gate (`configs/app.yaml → velocity_gating`) is what stops a car merely driving past a bay from claiming it.

### 2.6 How the system decides occupied or free

For each eligible track, `containment_score` measures how much of the vehicle's *ground footprint* lies inside a bay (not how much of the bay is covered — that would favour big bays). Rules, all from `configs/app.yaml → occupancy`:

- containment ≥ **0.45** → the vehicle claims that bay;
- best containment < **0.20** on every bay → the vehicle is in the aisle, no evidence;
- two bays each ≥ **0.20** → the vehicle is straddling a line, reported as a `violation`;
- assignment is greedy (`_assign`) so one car cannot occupy two bays.

The per-bay FSM then converts evidence into a committed state:

```
FREE → PENDING_OCCUPIED → OCCUPIED        (needs 0.6 s of continuous evidence)
OCCUPIED → PENDING_FREE → FREE            (needs 8.0 s with no vehicle in it)
any state → UNKNOWN                       (camera offline / no frames for OFFLINE_AFTER_S)
```

The asymmetry is deliberate and is the answer to the classic viva question: a wrong `OCCUPIED` is visible and corrects itself in a second, a wrong `FREE` double-books a bay. `PENDING_*` states exist only in memory — the database and `GET /slots` only ever carry committed states (`routers/slots.py:32-38`).

### 2.7 How data reaches the database and the frontend

Two consumers sit on one in-memory bus, so detection never waits on I/O:

- **`WebSocketHub`** (`api/live.py:199`) subscribes in-process and pushes each event to every connected dashboard (per-client queue of 200; a slow client drops its own messages and is counted in `/health → dropped_events`).
- **`PersistenceWorker`** (`services/persistence.py:407`) is a daemon thread that `drain()`s the bus in batches of up to 200 and writes `cameras`, `parking_slots`, `license_plates`, `vehicles`, `parking_sessions`, `slot_occupancy_history` and `detection_logs`; a failed event is retried on the next pass and at most 200 failure samples are kept, so one bad row never blocks the stream.

Only the server started with `--persist` runs the second consumer. `GET /slots` reads the **database** (committed rows); the socket carries **transitions**; `GET /overlay` and the MJPEG stream carry the **current frame**. That is why the dashboard seeds itself from REST and then stays current from the socket.

### 2.8 How the dashboard updates in real time

`Dashboard.tsx:65-79` builds one `LiveSocket`, subscribes a reducer, and calls `connect()`. `LiveSocket.open()` mints a **fresh** ticket per attempt (`POST /auth/ws-ticket`), because the server spends it on the handshake; messages are folded by `applySlotMessage` (`lib/slots.ts:40`), which ignores anything that is not a `channel: "slot"` event naming a bay this dashboard shows, and ignores a message that would not change a status. Counters are then re-derived from the bay list itself, so the cards and the map can never disagree.

### 2.9 Recorded video replay vs. a physical live camera — the real difference

Both run **the same pipeline code**; only the source and the clock differ.

| Aspect | Recorded replay (what runs today) | Physical live camera (code exists, not connected) |
|---|---|---|
| Source | `data/samples/lot_shot5.mp4`, `data/fixtures/gate_visit.mp4` | index `0` → `cv2.CAP_DSHOW`; `rtsp://…`/`http://…` → `cv2.CAP_FFMPEG` (`video/sources.py:39`) |
| Clock | `FrameGrabber` uses the **video timestamp** of each frame, so processing speed and clip speed stay comparable even when the decoder runs at 16 fps on a 30 fps clip | wall clock; a frame is "now" |
| Pacing | `pace_replay: true` throttles the loop so a clip does not run flat-out and flood the bus | no pacing needed |
| Looping | `loop: true` on both cameras; `first_pass_events_only: true` means pass 2+ streams pictures but does not re-emit the same visits | no loop; a missed frame is a missed event |
| Frames | decoded from disk; ~100 KB JPEG each | `cap.read()` can return `False`, can stall, and adds network latency and codec variance |
| Validity of the numbers | detection *accuracy* is real; **FPS and latency are not camera measurements** | FPS/latency become meaningful |
| What the server itself says | the startup banner prints: *"WARNING: these are FILE REPLAYS, not field cameras… no latency or FPS figure here describes a real camera."* (`serve.py`/`app.py` banner, seen today in the log) | the banner is not printed for a non-file source (`live_mode = not (replay and is_file_source)`, `pipeline/lot_pipeline.py:124`) |

To connect a real camera later, the change is configuration — `configs/app.yaml → api.pipeline.enabled[].source` (or the camera table's `source` field) pointing at `0` or an RTSP URL — plus re-drawn bay polygons for the new viewpoint (`scripts/calibrate.py`), because the geometry in `lot_a_cam1.json` is a picture of *this* clip.

---

## 5. Feature status table

| Feature | Actual implementation | Verification evidence (today) | Status | Remaining work |
|---|---|---|---|---|
| Admin login / authentication | Argon2id hashing, HS256 access (900 s) + refresh (43200 s) pair, revocation via `tokens_invalid_before`, login throttle 10/300 s, role gates | `POST /auth/login` → 200 with the token pair; `/auth/me` → `{"role":"ADMIN", "reveal_plates": true}`; unauthenticated `/health` → 401 `token_missing`; `audit_logs` gained two `login` rows (total 2050) | **Working** | Account self-service UI |
| Vehicle detection | YOLO11s, `classes [2,3,5,7]`, conf 0.35, CUDA+FP16 with CPU fallback | `/health → gpu.available true`, device *RTX 4050 Laptop*, torch 2.11.0+cu128; overlay returned 5 vehicle tracks, conf 0.46–0.94, real pixel boxes; 1,412 frames in 89 s | **Working on recorded real footage** | Field-camera validation |
| Vehicle tracking | ByteTrack through `model.track(persist=True)`; synthetic ids only when tracking is disabled | Track ids `2,3,4,211,212` held stable across three samples 2 s apart while boxes moved (id 211: `[17,224,250,341] → [80,235,394,407] → [100,258,378,503]`) | **Working** | ID-switch metric over long clips |
| Parking occupancy detection | footprint→bay containment + greedy assignment + FSM + velocity gate | Pipeline log `slot A2 -> OCCUPIED (track=4)` etc.; `/overlay summary` moved `B1 PENDING_FREE → OCCUPIED` between samples; 27 persisted history rows | **Working** | Re-calibrate per new viewpoint |
| Camera / video input | File, webcam-index and RTSP/URL branches in `open_capture`; `FrameGrabber` for read + pace | Both live cameras have `source` = a file; health shows `synthetic_replay false` for the real clip, `true` for the fixture | **Working (recorded only)** | Connect an RTSP source + re-calibrate |
| Real-time dashboard | TanStack Query seed + socket reducer + `countSlots`; honest status chips | `tsc` exit 0; 53 vitest tests pass; dev server 200 on 5173 | **Working** | No component-render tests (vitest runs in `node`, no jsdom) |
| WebSocket communication | `/ws/live` events + `/ws/cameras/{id}/overlay` boxes; one-time tickets (15 s); auto-reconnect except on fatal closes | 845 messages / 45 s, `dropped_events 0`, `ws_ticket_refusals 0`; socket refused `?token=` in favour of `?ticket=` | **Working** | Nothing outstanding |
| Parking sessions (entry/exit) | Gate line-crossing logic + in-memory session machine → `parking_sessions` | `GET /sessions` → total 4 incl. `status ANOMALY / anomaly_reason exit_without_entry`; `?status=ACTIVE` → total 2 | **Working (replay data)** | Real gate camera; durations populated |
| Entry/exit tracking over time | `slot_occupancy_history`, `detection_logs` | 27 + 190 rows; `GET /stats/occupancy` returned an hourly series ending today | **Working** | Retention policy |
| Number plate recognition | Full ANPR stage (RapidOCR PP-OCRv4 + validation + 7-frame voting) in `anpr/pipeline.py` | **`read_plates: false` on every served stream**; `license_plates` and `vehicles` = 0 rows; `/plates/search?q=MH1` → 200 `total 0` | **Implemented, currently disabled** | Enable on a gate camera whose clip has legible plates |
| Billing & revenue | Not implemented; API answers 501 by design | `GET /stats/revenue` → `501 not_implemented` ("needs tariffs and payments, which are a later phase") | **Not implemented** | Tariff model, payment source, chart |
| Reports & analytics | Occupancy over time; dwell-time statistics | `/stats/occupancy` 200 with hourly points; `/stats/duration` 200 but `sample_size 0` | **Partially working** | Needs populated session durations |
| Database persistence | `PersistenceWorker` batches of 200, per-event retry, Alembic 0001→0003 | Integrity `ok`, `fk_check` 0, alembic `0003`, row counts above; `--persist` banner "events ARE being persisted" on the live server | **Working** | None |
| Docker deployment | Compose with `db`, `api` (nvidia runtime, migrate-then-serve), `frontend-build`, `nginx` | `docker ps -a` shows only `smart-parking-db Up (healthy)`; images `deploy-api`, `deploy-nginx`, `deploy-frontend-build` exist but no containers | **Not running** | Bring the stack up (see §8.5); resolve the SQLite-in-bind-mount design first |

---

## 6. Technology stack

### Backend (Python 3.10.0)

| Technology | Purpose here | Source |
|---|---|---|
| **FastAPI + uvicorn** | HTTP surface, request validation, OpenAPI docs at `/docs`, WebSocket routes | `api/app.py:78 create_app`, `routers/*.py` |
| **Pydantic v2** | DTOs and the single error envelope `{"error":{"code","message"}}` | `api/schemas.py`, `api/app.py:206 register_handlers` |
| **python-jose (HS256)** | access/refresh token minting and verification | `api/security.py`, `api/deps.py:175 current_user` |
| **argon2-cffi** | password hashing with costs taken from config, not from a wrapper default | `api/security.py`, `core/config.py` |
| **Ultralytics YOLO11s + PyTorch 2.11 (CUDA 12.8)** | vehicle detection and ByteTrack re-identification | `detection/vehicle_detector.py:37 load_model`, `tracking/tracker.py:57` |
| **OpenCV 4.13** | frame capture (`CAP_DSHOW`/`CAP_FFMPEG`/file), drawing the annotated JPEG, the calibration window | `video/sources.py:39`, `parking/render.py`, `scripts/calibrate.py` |
| **Shapely / polygon maths** | point-in-polygon, Sutherland–Hodgman clipping, intersection and containment scores | `parking/geometry.py:54,94,129,141,160` |
| **SQLAlchemy 2 + Alembic** | ORM over 9 tables and migrations 0001–0003 | `db/models.py`, `db/session.py:87 build_engine`, `migrations/` |
| **SQLite (WAL)** — in use | the actual database: `sqlite:///data/smart_parking.db`, `busy_timeout=5000`, `foreign_keys=ON` | `configs/app.yaml:111` |
| **PostgreSQL 15** — configured, **not used** | optional `--profile postgres` service; switching is a URL change only | `deploy/docker-compose.yml`, `core/config.py :: database_url_for` |
| **python-multipart, httpx, pytest** | form parsing, test client, 646 tests | `pyproject.toml` extras |

### Frontend (Node 24, Vite 8)

React 19 + TypeScript, Vite dev proxy (`/api` → `http://localhost:8000`, `/ws` → `ws://localhost:8000`), TanStack Query v5 for REST, Tailwind v4, recharts for the (currently hidden) revenue chart, lucide-react icons, vitest 3.2.7 running in `node`, oxlint. Key files: `frontend/src/lib/api.ts` (token refresh + single-flight + `streamUrl`), `lib/websocket.ts` (`LiveSocket`, ticket minting), `lib/slots.ts` (`applySlotMessage`, `countSlots`), `components/Dashboard.tsx`, `components/SlotMap.tsx`, `components/CameraPreview.tsx`.

### Deployment

Docker Compose (`deploy/docker-compose.yml`) with `db` (Postgres, profile-gated), `api` (`runtime: nvidia`, `8000:8000`, mounts `../data` read-write and `configs`/`models`/`src`/`tests` read-only, command `migrate.py upgrade head && serve.py --persist`), `frontend-build` (one-shot emitter into `../frontend/dist`) and `nginx` (port 80). `deploy/nginx-site.conf` gives the MJPEG location its own block with `proxy_buffering off` (line ~50) and the WebSocket block its upgrade headers (line ~72), and deliberately keeps no CORS headers because the browser is same-origin.

---

## 7. Testing and verification results

Every command below was executed today. The database was **never** written by this audit: the live pipeline probes ran against a scratch SQLite copy on port 8010, started **without** `--persist`, and that copy was deleted afterwards.

### 7.1 Backend test suite

```
$ python -m pytest tests -o addopts="" -q -p no:warnings
646 passed in 85.38s (0:01:25)
exit=0
```

(26 test modules under `tests/unit`; the repo's `addopts` contains `-q`, so a plain `pytest -q` runs twice-quiet and prints no summary line — that is why `-o addopts=""` is used.)

### 7.2 Frontend: types, tests, production build

```
$ npx tsc --noEmit -p tsconfig.json
tsc_exit=0

$ npx vitest run
 ✓ src/lib/api.test.ts (21 tests) 36ms
 ✓ src/lib/websocket.test.ts (15 tests) 467ms
 Test Files  3 passed (3)
      Tests  53 passed (53)
   Duration  2.37s
vitest_exit=0

$ npm run build
dist/assets/index-t80G1Wbx.js   632.42 kB │ gzip: 189.33 kB
(!) Some chunks are larger than 500 kB after minification.
✓ built in 8.29s
build_exit=0
```

`vitest` runs in the `node` environment without jsdom, so these are contract/logic tests (URL building, token handling, ticket use, slot folding) — **not** component-render tests. That gap is listed in §10 and in the limitations.

### 7.3 Live AI pipeline, detected through an isolated scratch instance

Start (no `--persist`, so neither the live nor the recovered database receives a write):

```
$ python scripts/serve.py --port 8010 --url sqlite:///…/data/out/report_check.db
smart-parking API v0.1.0 - /api/v1  (read-only: events are NOT being persisted)
streams: 2 camera thread(s) - lot_cam1, gate_visit
WARNING: these are FILE REPLAYS, not field cameras…
ANPR: configured, off on every served stream (read_plates: false)
```

**`GET /api/v1/health`** (after 89 s):

```json
{"status":"ok","database":"ok","anpr_enabled":true,
 "gpu":{"available":true,"torch_version":"2.11.0+cu128","cuda_version":"12.8",
        "device":"NVIDIA GeForce RTX 4050 Laptop GPU","memory_total_mib":6140,"memory_free_mib":4969},
 "cameras":[{"camera_id":"lot_cam1","streaming":true,"frames_available":true,
             "synthetic_replay":false,"read_plates":false,"frames_processed":1412},
            {"camera_id":"gate_visit","streaming":true,"frames_available":true,
             "synthetic_replay":true,"read_plates":false,"frames_processed":72}],
 "frames_in":2744,"events_out":845,"dropped_events":0,"ws_ticket_refusals":0,"uptime_s":89.31}
```

`lot_cam1` is **real footage** (`synthetic_replay: false`), so its boxes come from YOLO, not from a labelled file.

**`GET /cameras/lot_cam1/overlay` — three samples 2 s apart** (proof of detection + tracking + live FSM):

| Sample | seq | track ids held | evidence of motion |
|---|---|---|---|
| 1 | 10276 | 2, 3, 4, 211, 212 | id 211 box `[17.2, 224.7, 250.2, 341.1]`; bay summary `B1: PENDING_FREE` |
| 2 | 10316 | 2, 3, 4, 211, 212 | id 211 box `[80.2, 235.6, 394.0, 407.4]`; summary `B1: OCCUPIED` |
| 3 | 10356 | 2, 3, 4, 211, 212 | id 211 box `[99.9, 258.7, 378.0, 503.3]`; confidences 0.46–0.94 |

Same five ids, different geometry → the tracker is re-identifying; `PENDING_FREE → OCCUPIED` inside 2 s → the FSM is running live. `summary` in the same payload: `{"total":5,"occupied":3,"available":2,"unknown":0}`.

**`GET /cameras/lot_cam1/stream.mjpg`** — 8-second capture:

```
content-type: multipart/x-mixed-replace; boundary=smartparkinglot_cam1
parts received in ~8 s : 37          (≈4.6 fps, config api.mjpeg.fps: 5.0)
distinct JPEG hashes   : 37 of 37    (no frame repeats)
JPEG size              : ~99–104 KB each; decoded shape (850, 478, 3) = source geometry
```

One of those frames is saved at [`figures/lot_cam1_annotated_frame.jpg`](figures/lot_cam1_annotated_frame.jpg): boxes, tracking ids, bay polygons and the HUD are burned in by `parking/render.py`, i.e. exactly what the pipeline concluded, not a redraw.

### 7.4 REST surface, all with a real token

| Endpoint | Result today |
|---|---|
| `GET /health` | 200 (body above) |
| `GET /cameras` | 200, 4 cameras with `live` blocks; `gate_cam1`/`lot_cam1_shot1` → `streaming: false` |
| `GET /slots?camera_id=lot_cam1` | 200, 5 committed rows (A1 FREE, A2/B1/B2 OCCUPIED, B3 FREE) with polygons |
| `GET /lots/lot_cam1/summary` | `{"total":5,"occupied":3,"available":2,"reserved":1,"unknown":0,"occupancy_rate":0.6,"from_live_frames":false}` |
| `GET /stats/occupancy` | 200, hourly series built from `slot_occupancy_history` |
| `GET /stats/duration` | 200, all nulls, `sample_size: 0` |
| `GET /stats/revenue` | **501 `not_implemented`** |
| `GET /sessions?limit=3` | 200, `total: 4`, includes an `ANOMALY` / `exit_without_entry` |
| `GET /sessions?status=ACTIVE` | 200, `total: 2` |
| `GET /plates/search?q=MH1` | 200, `total: 0` (table empty — ANPR off) |
| `GET /plates/search?plate=AB` | 422 `query_too_short` (the length guard is on the *normalised* fragment, `routers/plates.py:35-41`) |
| `GET /audit-logs?limit=3` | 200, `total: 2050`, newest rows are the two `login` entries this audit created |
| `GET /auth/me` | 200, `{"role":"ADMIN","reveal_plates":true}` |
| no-token request to a protected route | 401 `token_missing` (verified against the **live** server on :8000) |

### 7.5 Real-time verification: socket events and the debounce, measured

Captured from the moment a fresh pipeline started (`evidence/ws_first_pass_events.json`, 45 s window):

```
messages: 845   by (channel,type):
  session/hello            1
  slot/slot_snapshot       5      <- first committed reading of every bay
  slot/slot_occupied       3      <- real FREE->OCCUPIED transitions
  violation/violation      1
  summary/summary        834
dropped_events: 0    ws_ticket_refusals: 0
```

The three transitions, verbatim:

```json
{"type":"slot_snapshot","channel":"slot","camera_id":"lot_cam1","ts":1792000904.101651,
 "payload":{"slot_id":"A2","status":"FREE","track_id":4,"score":0.962}}
{"type":"slot_occupied","channel":"slot","camera_id":"lot_cam1","ts":1792000904.701651,
 "payload":{"slot_id":"A2","status":"OCCUPIED","track_id":4,"score":0.938}}
{"type":"slot_occupied","channel":"slot","camera_id":"lot_cam1","ts":1792000907.668318,
 "payload":{"slot_id":"B1","status":"OCCUPIED","track_id":5,"score":0.985}}
```

`…904.701 − …904.101 = 0.600 s` — exactly `occupancy.occupied_after_s: 0.6`: evidence must be continuous for that long before a bay flips, which is why no bay flickers between samples. (`ts` is the pipeline's frame clock derived from the clip's own timestamps, which is why it differs from wall-clock audit timestamps.)

### 7.6 Docker and the two-writer guard

```
$ docker ps -a
NAMES             STATUS
smart-parking-db  Up 52 minutes (healthy)     0.0.0.0:5432->5432/tcp

$ docker images | grep deploy
deploy-frontend-build:latest  583MB
deploy-nginx:latest           93.6MB
deploy-api:latest             13.6GB          (images built, containers absent)
```

Guard, executed today: with `serve.py --persist` already holding `data/smart_parking.db`, a second start against the same file exits **1** with the "two writers" refusal rather than corrupting anything.

### 7.7 Checks that were NOT performed (stated plainly)

- The logged-in `/health` of **your own** :8000 server — requires your admin credentials, which this audit deliberately does not hold or request. Its HTTP surface was verified (listening, correct 401 envelope, static shell served by :5173).
- A second browser-driven dashboard session on your live instance (the token-folding and preview paths were verified earlier in this project against an isolated instance, and their logic is covered by the 53 vitest tests, but no fresh click-through on your logged-in dashboard was executed today).
- Any accuracy measurement (precision/recall/mAP) against hand-labelled ground truth — the pipeline's decisions were verified by *observation*, not scored.
- ANPR accuracy in the running service — it is switched off.
- Long-run stability, concurrent-viewer load, and failover of the Postgres profile.

---

## 8. How to run and demonstrate the project

> **Rule that protects your data:** one SQLite writer at a time. Either the host server **or** the Docker `api` service may hold `data/smart_parking.db`, never both. Today the **host** server is already running it, so before starting anything new, check §8.1.

### 8.1 Check what is already running — PowerShell

```powershell
netstat -ano | Select-String ":8000|:5173" | Select-String LISTENING
```

If `127.0.0.1:8000` is already LISTENING (it is today, PID 17348), the backend and its writers are up. Do not start another.

### 8.2 Start the backend — **PowerShell** (host run, the current mode)

```powershell
cd "C:\Users\Saurabh\Downloads\Parking ai sysytem"
python scripts/serve.py --persist
```

Expected banner lines: `smart-parking API v0.1.0 - /api/v1`, `database: sqlite:///data/smart_parking.db`, `streams: 2 camera thread(s) - lot_cam1, gate_visit`, and the FILE REPLAYS warning. Port 8000; docs at `http://127.0.0.1:8000/docs`.

To read `/health` from the terminal you need a token, because the route is deliberately protected (an unauthenticated call gets `401 token_missing`, verified today):

```bash
T=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
      -H 'content-type: application/json' \
      -d '{"email":"admin@parking.local","password":"…"}' \
    | python -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8000/api/v1/health
```

Type the password at the prompt; do not commit it, screenshot it, or leave it in shell history. The body you get back has the same shape as the one in §7.4/§7.5, since it is the same code.

If you want a *second* instance for experiments without touching your data (this is exactly what this audit did):

```powershell
python scripts/migrate.py upgrade head --url "sqlite:///C:\path\to\scratch.db"   # after copying the db file
python scripts/serve.py --port 8010 --url "sqlite:///C:\path\to\scratch.db"      # no --persist
```

### 8.3 Migrations, accounts, and the secret — **Git Bash** or PowerShell (both work)

```bash
python scripts/migrate.py upgrade head      # or: current   /   downgrade -1
python scripts/dev_secret.py                # writes data/.secret once
python scripts/create_user.py --email admin@parking.local --role admin   # prompts twice, never on the command line
python scripts/create_user.py --list
```

Never pass `--password` on the command line; it would land in shell history.

### 8.4 Start the dashboard — **Git Bash** (or PowerShell, same syntax)

```bash
cd "/c/Users/Saurabh/Downloads/Parking ai sysytem/frontend"
npm install          # only the first time
npm run dev          # http://localhost:5173
```

`vite.config.ts` proxies `/api` → `http://localhost:8000` and `/ws` → `ws://localhost:8000`, so the dev server and the backend need no CORS changes. Sign in with the account from §8.3.

Checks inside the page: header chip says **Live**; the bay map colours 3 of 5 bays occupied with none marked "Not Reported" for `lot_cam1`; the camera card shows the annotated picture with a **Streaming** chip; the counters add up to the map.

### 8.5 Demonstrate the AI pipeline on its own (no web layer) — **Git Bash**

```bash
# the occupancy pipeline on the lot clip, opening an OpenCV window
python scripts/run_occupancy.py --camera lot_cam1 --show

# the gate entry/exit pipeline on the fixture clip
python scripts/run_gate.py --camera gate_visit --show

# detection only, to see raw YOLO output
python scripts/run_detect.py --source data/samples/lot_shot5.mp4 --show
```

`--show` needs a desktop session; use `--save-frames <dir>` if no window can open.

### 8.6 Stop the services safely

- Backend: `Ctrl+C` in the window running `serve.py`, then confirm the port is free with §8.1. Let the process exit on its own — killing it mid-batch only loses unpersisted events, it does not corrupt the file (WAL).
- Frontend: `Ctrl+C` in the Vite window.
- Docker (if you ever bring the stack up): `docker compose down` — **never** `down -v` (see §10).

### 8.7 Docker path (currently *not* running) — **Git Bash only**

```bash
cd "/c/Users/Saurabh/Downloads/Parking ai sysytem/deploy"
docker compose up -d db api nginx
docker compose --profile postgres up -d      # the Postgres variant
docker compose up --build frontend-build     # one-shot: emits ../frontend/dist for nginx
docker compose ps
```

Before using the Docker `api` service against SQLite, stop the host server: the compose file bind-mounts `../data` read-write, and that shared-file setup is exactly what corrupted the database on 13 October. For a Docker demo, prefer the `--profile postgres` database so the API has its own writer.

---

## 9. Presentation-ready deliverables

| # | Deliverable | File |
|---|---|---|
| 1 | Complete project report (this document, §1–§8) | `docs/project_report.md` |
| 2 | System architecture diagram, Mermaid | `docs/architecture.md` |
| 3 | Working flow diagram, Mermaid | `docs/working_flow.md` |
| 4 | Feature status + verification table | §5 of this file |
| 5 | Simple viva explanation | `docs/viva_and_demo.md` §A |
| 6 | 5-minute demonstration script | `docs/viva_and_demo.md` §B |
| 7 | Five likely viva questions with answers | `docs/viva_and_demo.md` §C |
| 8 | Known limitations and future improvements | `docs/viva_and_demo.md` §D |
| — | Raw evidence: WebSocket capture, annotated frame | `docs/evidence/`, `docs/figures/` |

---

## 10. One defect found during this audit

`frontend/src/components/Dashboard.tsx` mounts `<CameraPreview cameraId={DEFAULT_CAMERA} />` **twice** (lines 142–147, two adjacent blocks with different comments). Consequence: the dashboard opens **two** MJPEG connections to `lot_cam1` for every operator and draws the card twice, doubling JPEG work on the GPU-bound server. It is a one-block deletion; the fix was not applied because this audit was asked to document the project, not to change it. Recommend removing the duplicate before the demonstration.

Other items to schedule (not defects, gaps): no component-render tests (§7.2), the 632 kB single JS chunk, and `lot_cam1_shot1` rows that will read `UNKNOWN` forever unless a pipeline is enabled for them or the rows are retired.
