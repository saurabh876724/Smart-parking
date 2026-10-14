# Section 9 (items 5–8) — Viva Explanation, Demonstration Script, Q&A, Limitations

Companion to [`project_report.md`](project_report.md) (§1–§8), [`architecture.md`](architecture.md) (§3) and [`working_flow.md`](working_flow.md) (§4).

---

## A. The project in simple language (for the viva)

**One paragraph.** I built a system that watches a car park through an overhead camera and tells an operator, on a web dashboard, which parking bays are occupied and which are free — without any sensor in the ground. It detects vehicles with a neural network, follows the same vehicle from frame to frame so it is counted once, works out geometrically which bay each vehicle is standing in, and only then changes the bay's status after the evidence has held for a fixed time. Everything it decides is written to a database as history, and pushed to the browser the moment it changes.

**The chain, in the words I would say out loud.**
1. A frame comes in from the video source.
2. YOLO answers *"there is a vehicle here, and here, and here"* with a box and a confidence. It knows nothing about parking.
3. ByteTrack answers *"this box is the same car as last frame — call it id 211"*. Without this a parked car would look like a new arrival every frame.
4. Geometry answers *"how much of that car's ground footprint sits inside bay A2?"* Each bay is a four-point polygon I drew with the calibration tool.
5. The occupancy engine answers *"one car, one bay"* — the strongest claim wins, and a car over two lines is reported as a violation rather than being forced into one bay.
6. The state machine answers *"I will not change the sign until the evidence has held"*: 0.6 seconds to say occupied, 8 seconds to say free.
7. The event bus carries the decision to two listeners: the WebSocket, which tells the browser now, and the persistence worker, which writes the history.
8. The dashboard seeds itself from the REST API and then stays current from the socket, and the camera card shows the very frame the detector reasoned about.

**What is real and what is staged — say this before anyone asks.** The footage is a **recorded clip**, not a camera I have wired in. The detection, tracking, geometry and state logic run for real on that clip (YOLO on the GPU, real confidences, real transitions). The gate camera's clip is a **rendered fixture** whose boxes are replayed from a labelled file, so anything from it is marked `SYNTHETIC` and is not evidence of detection accuracy. Number-plate reading is fully implemented but switched off on the streams I serve, and billing is deliberately not implemented — the API answers *501 Not Implemented* rather than showing a fake chart.

**Why the numbers in the UI are trustworthy.** A bay that the camera cannot report is shown as **Not Reported**, never as free — because a camera failure must not look like spare capacity. The counters are derived from the bay list itself, so the cards and the map can never disagree. The header says **Live** only when the event socket is actually open, and it says `Reconnecting` or `Not live` otherwise.

---

## B. Five-minute demonstration script

Run this only after §8 of the report has the backend and frontend up (`:8000` with `--persist`, `:5173`). Everything below is a click or a one-line command; the *expected* results are the ones measured in this audit on 14 October 2026.

| Time | Action | What to say / what must appear |
|---|---|---|
| 0:00–0:30 | Open `http://localhost:5173`, sign in with the admin account | Sign-in screen; on success the dashboard loads. Say: *"access token lives 15 minutes, refresh token 12 hours, passwords are Argon2id-hashed."* |
| 0:30–1:15 | Point at the header chip and the five cards | Chip reads **Live** — that is the `/ws/live` socket, separate from the page. Cards: **Total 5 / Occupied 3 / Available 2 / Not Reported 0 / Active Sessions** from the server's own filter, not from the rows on the page |
| 1:15–2:15 | Scroll to **Camera Feed** | A moving annotated picture, chip **Streaming**. Zoom in verbally: *"the boxes, the tracking ids, the bay polygons and the TOTAL/OCCUPIED/FREE text are drawn by the pipeline itself — this is the frame the detector worked on, not a second copy."* |
| 2:15–3:00 | Open the terminal, run the overlay probe twice | `curl -H "Authorization: Bearer $T" http://127.0.0.1:8000/api/v1/cameras/lot_cam1/overlay` (twice, 2 s apart). Say: *"same track ids 2, 3, 4, 211, 212, different pixel boxes — that is ByteTrack. And look, B1 moved from PENDING_FREE to OCCUPIED between the two calls."* |
| 3:00–3:45 | Show the socket is real | `curl -X POST -H "Authorization: Bearer $T" http://127.0.0.1:8000/api/v1/auth/ws-ticket` → prints a one-time ticket. Say: *"the socket cannot set a header, so it carries a ticket that dies after 15 seconds or after one use — never the access token."* Optionally replay `docs/evidence/ws_first_pass_events.json` and read the two lines 0.600 s apart for bay A2 |
| 3:45–4:30 | Show persistence and history | `curl -H "Authorization: Bearer $T" "http://127.0.0.1:8000/api/v1/slots?camera_id=lot_cam1"` → committed statuses with polygons; then `/lots/lot_cam1/summary` → `occupancy_rate 0.6`; then `/stats/occupancy` → the hourly series built from `slot_occupancy_history` (27 rows today) |
| 4:30–5:00 | Close with the honest boundaries | `curl -i -H "Authorization: Bearer $T" http://127.0.0.1:8000/api/v1/stats/revenue` → **501**. Say: *"billing is a later phase — I refuse to invent numbers. Recorded replay, not a field camera; ANPR implemented but disabled; only 2 of 4 configured cameras are streamed, and the other camera's bays honestly read UNKNOWN."* |

**Getting the `$T` token for the curl steps (do this before the demo, not during it):** sign in the browser, open DevTools → Network → any `/api/v1/` request → copy the `Authorization` header value after `Bearer `. Or mint one from the terminal, which also shows the API works:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@parking.local","password":"…"}' | python -c "import json,sys;print(json.load(sys.stdin)['access_token'])"
```

Type the password at the prompt rather than leaving it in shell history, and never paste a token into a screenshot — it is a live credential for 15 minutes.

**Backup if the browser is unavailable:** `python scripts/run_occupancy.py --camera lot_cam1 --show` demonstrates the same pipeline with an OpenCV window, and `GET /api/v1/health` (logged in) proves the GPU and frame counters.

**Do not do during a demo:** do not start a second `serve.py` on the same database (the guard will refuse, exit 1, and it looks like a crash), and do not run the Docker `api` service while the host server holds `data/smart_parking.db`.

---

## C. Five likely viva questions — and the answers I can defend

**Q1. Why do you need object tracking if you already have detection?**
Because occupancy is a statement about a *place*, made over *time*. Detection gives a box per frame; without persistent ids the FSM cannot tell whether the same car is still in bay A2 or a different one just arrived, so it would either flicker every frame or never commit. ByteTrack (`tracking/tracker.py:57`, `persist=True`) gives the id that makes "occupied for 0.6 s" mean something. In today's run the same five ids held across samples 2.7 s apart while their boxes moved ~80 px.

**Q2. How do you decide a car belongs to a particular bay, and why not just check whether the box overlaps the bay?**
I compute how much of the vehicle's **ground footprint** — the bottom 40 % of the box, which is the part actually on the tarmac — lies **inside** the bay polygon (`parking/geometry.py:160 containment_score`, Sutherland–Hodgman clipping at `:94`). Bay-denominated coverage would let a huge bay "own" a car parked in the aisle beside it, and would let one car claim two bays; containment plus a greedy one-car-one-bay assignment (`occupancy_engine.py`) fixes both. Thresholds: ≥0.45 to claim, <0.20 on every bay means it is in the aisle, and ≥0.20 on two bays simultaneously is reported as a straddling **violation** instead of being forced into one.

**Q3. Your system says a bay is free and two cars are sent in. How do you prevent that, and what about a camera failure?**
Two mechanisms. First, the FSM is deliberately asymmetric: it takes **0.6 s of continuous evidence** to say `OCCUPIED` but **8.0 s with nothing in it** to say `FREE` (`configs/app.yaml:21-22`, comment: *"a false FREE double-books a slot"*) — a brief occlusion or a dropped detection burst cannot empty a bay. Second, a camera that stops reporting does **not** produce `FREE`: `mark_all(SlotStatus.UNKNOWN)` runs after `OFFLINE_AFTER_S` with no frames (`lot_pipeline.py:188-195`), and `UNKNOWN` is never folded into "available" — it is counted in its own **Not Reported** bucket in the UI (`lib/slots.ts:13-17`).

**Q4. Why is there an event bus instead of the pipeline just writing to the database and calling an API?**
So that inference never waits on I/O, and so the two consumers can fail independently. `InMemoryEventBus` (`core/events.py:28`) is fed by the pipeline thread; the `WebSocketHub` pushes each event to every dashboard with a bounded 200-message per-client queue (a slow browser drops **its own** backlog, and `dropped_events` in `/health` counts it — today 845 events out, 0 dropped), while a separate daemon thread batches up to 200 events per write and retries an individual failure without stalling the rest (`services/persistence.py`). The same design lets me run the API with no persistence at all — which is exactly how this audit was performed without touching your data.

**Q5. What are the weaknesses of your system, and how would you measure them?**
Four honest ones. (a) It is validated on recorded video, so accuracy on a real installation is unmeasured — I would label frames by hand and report precision/recall and per-bay accuracy against ground truth (the repo's own acceptance target is ≥95 % bay accuracy with no flicker, `configs/app.yaml:2`). (b) Containment thresholds and the 30 px/s velocity gate are tuned for *this* viewpoint at 478×850 and must be re-measured per camera. (c) Occlusion and a car parked half outside a polygon are the geometric failure modes, and there is no re-identification across cameras. (d) There are no component-render tests (vitest runs in Node without jsdom) and no throughput test with many concurrent viewers. Also note `GET /stats/duration` currently returns `sample_size 0` and `license_plates` is empty — both are configuration/data facts, not silent bugs, and they are visible in the report's feature table.

---

## D. Known limitations and future improvements

### D.1 Limitations that exist today (all verified)

| # | Limitation | Evidence | Is it a bug? |
|---|---|---|---|
| 1 | Input is **recorded video only**; no physical camera connected | `api.pipeline.enabled` lists two file sources; `serve.py` prints the FILE REPLAYS warning | No — staged scope |
| 2 | FPS/latency numbers describe a decoder + GPU, not a field camera | 1,412 frames in 89 s on a 30 fps clip | No — must be stated in the viva |
| 3 | `gate_visit` boxes come from a labelled fixture, not from inference | `/health → synthetic_replay: true` | No — clearly marked, drawn as `SYNTHETIC` |
| 4 | ANPR disabled on served streams → `license_plates` and `vehicles` are empty, plate search returns nothing | `read_plates: false`, `GET /plates/search?q=MH1` → `total 0` | No — configuration |
| 5 | **No billing**: `/stats/revenue` → 501; chart hidden by design | response captured today | No — later phase |
| 6 | Session durations unusable: `/stats/duration` → `sample_size 0` | response captured today | Data gap, not logic gap |
| 7 | Two of four configured cameras are never started; their 5 bay rows sit at `UNKNOWN` permanently | `/cameras` shows `streaming: false`; DB rows | Honest behaviour; the rows should be retired or given a pipeline |
| 8 | Looped replay emits events only on the **first pass** | `first_pass_events_only: true`; after pass 1 the socket carries `summary` only | By design; a demo must restart the server to show transitions |
| 9 | **`CameraPreview` is mounted twice in `Dashboard.tsx`** (lines 142–147) → two MJPEG connections per operator, duplicated card | read of the source during this audit | **Yes — a defect; fix before the demo** |
| 10 | No component-render tests; the 53 frontend tests are logic/contract tests in Node | vitest config `environment: node`, 3 test files | Coverage gap |
| 11 | Single JS bundle of 632 kB (gzip 189 kB) with a Vite chunk-size warning | `npm run build` output today | Performance nicety |
| 12 | Docker stack not running: only `smart-parking-db` is up, and Postgres is unused while SQLite carries the data | `docker ps -a`, `configs/app.yaml:112` | Environment state |
| 13 | SQLite under a bind mount shared between host and container is the exact pattern that corrupted the database on 13 October | incident folder preserved; guard added to `serve.py` (verified refusing a second writer, exit 1) | Mitigated, not removed |
| 14 | Thresholds (`0.45 / 0.20 / 30 px/s / 0.6 s / 8.0 s`) are tuned for one viewpoint and are not auto-calibrated | comments in `configs/app.yaml:28-38` | Known scope |

### D.2 Future improvements, in the order I would do them

**Immediately (code hygiene, before the submission)**
1. Delete the duplicated `<CameraPreview/>` mount (`Dashboard.tsx:142-147`) — one line block.
2. Retire `lot_cam1_shot1`'s five rows or enable a pipeline for it, so the map stops showing permanently-unknown bays.
3. Add a short "replay" note in the dashboard header while `synthetic_replay`/file sources are the norm, so a viewer cannot mistake the demo for a field deployment.

**Short term (make it demonstrably correct)**
4. Hand-labelled ground-truth frames per clip → report per-bay accuracy, precision/recall of detection, and an ID-switch rate for the tracker. This turns "it looks right" into a number.
5. Component-render tests with jsdom (or Playwright) covering: socket message → bay recolour, `UNKNOWN` not counted as available, and `CameraPreview` phase chips.
6. Concurrent-viewer load test (N viewers on the MJPEG + socket) to set a real capacity figure for `ws_queue_size` and `api.mjpeg.fps`.
7. Code-split the dashboard so the camera card and the charts load lazily.

**Medium term (feature completion)**
8. **Live camera integration**: an RTSP source through `open_capture`, re-calibration per camera, and a documented `source:` per camera in the compose deployment; measure real end-to-end latency at that point.
9. **Billing and revenue**: tariff model (per hour / per class / free bays), payment source, then replace the 501 with a real aggregate and re-enable the chart.
10. **ANPR on a gate camera** whose clip actually has legible plates, then link `parking_sessions.plate` to the lot occupancy so a car can be followed from entry to bay.
11. Per-camera threshold profiles + a calibration-measurement script so `velocity_gating.max_parking_velocity` and the containment numbers are derived from the footage instead of hand-set.
12. Multi-camera lot view (one dashboard for several cameras) with a shared bay namespace.

**Long term (operational maturity)**
13. Move to Postgres as the default writer for deployments (the profile already exists) so the two-writer hazard is structurally impossible.
14. Retention/archival policy for `detection_logs` and `slot_occupancy_history` (190 and 27 rows today; a 24/7 camera produces a row per transition per bay per day).
15. Alerting on `dropped_events`, `ws_ticket_refusals` and camera staleness — `/health` already exposes all three.
16. HTTPS by default once certificates are available (the nginx config keeps 443 unmapped until then, on purpose).

---

## E. One-page summary to carry into the viva

- **What it is:** camera-based bay occupancy for a car park, with a live dashboard and persisted history.
- **Pipeline:** OpenCV frame → YOLO11s detection → ByteTrack ids → footprint containment vs hand-drawn polygons → greedy one-car-one-bay → slot FSM (0.6 s / 8.0 s debounce, `UNKNOWN` for silence) → event bus → WebSocket + batched persistence.
- **Stack:** Python 3.10, FastAPI, SQLAlchemy + Alembic 0003, SQLite (WAL), Ultralytics/PyTorch on CUDA, OpenCV, React 19 + TypeScript + Vite, TanStack Query, Tailwind, Docker + nginx (written, not running).
- **Numbers I can quote:** **646** backend tests, **53** frontend tests, `tsc` clean, build 632 kB, integrity `ok`, alembic `0003`, 1,412 frames in 89 s, 37 MJPEG frames in 8 s, 845 socket events in 45 s with 0 dropped, occupancy commit in **0.600 s**, 5 bays (3 occupied / 2 free / 0 not-reported) for the streamed camera.
- **Boundaries I will state first:** recorded replay not a field camera; ANPR off; billing 501; two of four cameras not streamed.
- **The defect I found while auditing:** a duplicated camera card in `Dashboard.tsx`.
