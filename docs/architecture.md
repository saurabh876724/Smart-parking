# Section 3 — System Architecture (as actually implemented)

Every node below exists in the repository and was exercised on 14 October 2026, unless it is drawn in the **planned / not running** lane at the bottom. Arrows follow the real direction of data — video → inference → geometry → state → bus → (socket, database) → browser.

---

## 3.1 Architecture diagram

```mermaid
flowchart TD
    classDef source fill:#0f172a,color:#fff,stroke:#334155
    classDef ai fill:#1d4ed8,color:#fff,stroke:#1e40af
    classDef state fill:#065f46,color:#fff,stroke:#064e3b
    classDef api fill:#7c3aed,color:#fff,stroke:#5b21b6
    classDef store fill:#b45309,color:#fff,stroke:#92400e
    classDef web fill:#0e7490,color:#fff,stroke:#155e75
    classDef absent fill:#f1f5f9,color:#475569,stroke-dasharray:6 4

    subgraph SRC["Video input — file replay today"]
        V1["Recorded lot clip<br/>data/samples/lot_shot5.mp4<br/>camera lot_cam1, REAL footage"]:::source
        V2["Rendered gate fixture<br/>data/fixtures/gate_visit.mp4<br/>+ gate_visit_tracks.json<br/>SYNTHETIC, labelled boxes"]:::source
    end

    SRC --> CAP

    CAP["OpenCV capture + FrameGrabber<br/>video/sources.py open_capture<br/>video/grabber.py<br/>replay clock, pace_replay, loop"]:::ai
    CAP --> LOT["Lot pipeline loop<br/>pipeline/lot_pipeline.py run<br/>frame_stride, velocity gate"]:::ai
    CAP --> GATE["Gate pipeline loop<br/>pipeline/gate_pipeline.py<br/>line crossing, dwell_exit_s"]:::ai

    LOT --> DET["YOLO11s vehicle detection<br/>detection/vehicle_detector.py<br/>classes car/motorcycle/bus/truck, conf 0.35<br/>CUDA + FP16, cpu fallback"]:::ai
    GATE --> DET
    DET --> TRK["ByteTrack re-identification<br/>tracking/tracker.py persist=True<br/>stable track ids across frames"]:::ai
    V2 -. labelled boxes bypass YOLO .-> TRK

    TRK --> GEO["Bay geometry<br/>configs/slots/lot_a_cam1.json<br/>parking/geometry.py containment_score<br/>occupy 0.45, aisle 0.20, straddle 0.20"]:::state
    GEO --> OCC["Occupancy engine, greedy assign<br/>parking/occupancy_engine.py"]:::state
    OCC --> FSM["Per-bay finite state machine<br/>parking/slot_fsm.py<br/>FREE / PENDING_OCCUPIED / OCCUPIED /<br/>PENDING_FREE / UNKNOWN<br/>0.6 s to occupy, 8.0 s to free"]:::state
    FSM --> BUS["In-memory event bus<br/>core/events.py InMemoryEventBus<br/>slot_snapshot, slot_occupied, slot_freed,<br/>summary, vehicle_entered, vehicle_exited,<br/>plate_read, violation"]:::state

    FSM --> REN["Frame annotation<br/>parking/render.py<br/>boxes + track ids + polygons + HUD"]:::ai
    REN --> HUB["FrameHub (latest frame per camera)<br/>api/live.py"]:::api

    BUS --> WS["WebSocketHub<br/>api/live.py per-client queue 200"]:::api
    BUS --> PERS["PersistenceWorker daemon thread<br/>services/persistence.py<br/>batch 200, per-event retry<br/>ONLY when serve.py --persist"]:::store

    PERS --> DB[("SQLite, journal_mode=WAL<br/>data/smart_parking.db<br/>Alembic 0003<br/>cameras, parking_slots, parking_sessions,<br/>slot_occupancy_history, detection_logs,<br/>license_plates, vehicles, users, audit_logs")]:::store

    DB --> FAST["FastAPI under /api/v1<br/>api/app.py create_app<br/>auth, cameras, slots, sessions,<br/>plates, stats, health"]:::api
    HUB --> FAST
    WS --> FAST

    FAST --> NGINX["Nginx reverse proxy (port 80)<br/>deploy/nginx-site.conf: /api/, stream.mjpg<br/>with proxy_buffering off, /ws/ with upgrade"]:::web
    FAST --> VITE["Vite dev proxy (port 5173)<br/>/api to 8000, /ws to ws:8000"]:::web

    NGINX --> RE
    VITE --> RE
    RE["React 19 + TypeScript dashboard<br/>App.tsx auth gate, Dashboard.tsx<br/>SlotMap, CameraPreview, LiveChip<br/>TanStack Query + lib/websocket.ts"]:::web
    RE -->|"Authorization: Bearer access token"| FAST
    RE -->|"?token= only for the img stream"| FAST
    RE -->|"POST /auth/ws-ticket then WS /ws/live?ticket="| WS
    RE -->|"img src /stream.mjpg?token="| HUB
```

**Dashed = optional or conditional path.** `license_plates` and `vehicles` are written **only** when a stream has `read_plates: true`; on every served stream today it is `false`, so those two tables stay empty while the rest of the chain runs.

---

## 3.2 Planned / not running — deliberately kept out of the main diagram

```mermaid
flowchart LR
    classDef absent fill:#f8fafc,color:#334155,stroke:#94a3b8,stroke-dasharray:6 4

    CAM["Physical camera RTSP or webcam index<br/>code path exists in video/sources.py,<br/>NOT configured, NOT connected"]:::absent
    ANPR["ANPR on a live stream<br/>anpr/pipeline.py fully implemented,<br/>read_plates false on every served clip"]:::absent
    BILL["Billing and revenue<br/>no tariff or payment model;<br/>stats/revenue answers 501"]:::absent
    PG["PostgreSQL profile<br/>declared in compose under --profile postgres,<br/>container not running; app uses SQLite"]:::absent
    DOCKER["Docker api / nginx / frontend-build services<br/>images built, containers absent"]:::absent
    EXTRA["Cameras gate_cam1, lot_cam1_shot1<br/>rows exist, not in api.pipeline.enabled,<br/>so their bays read UNKNOWN"]:::absent
```

Nothing in this second diagram may be drawn into the first for the viva: the system today reads **recorded video**, **does not** read plates on the streams it serves, **cannot** bill, and **is not** deployed through nginx.

---

## 3.3 Component-by-component explanation, in simple language

**1. Video input.** Two clips on disk are opened as if they were cameras. `open_capture` in `video/sources.py:39` decides the kind: a plain number means a webcam index (`CAP_DSHOW`), a URL means a network camera (`CAP_FFMPEG`), anything else is a file. Both live entries are files. `FrameGrabber` adds the replay clock and the pacing so a decoder running at 16 fps does not behave as if the clip were 16 fps.

**2. Two pipeline loops.** `lot_pipeline.py` handles a parking-lot view and produces bay statuses; `gate_pipeline.py` handles an entrance view and produces vehicle entry/exit sessions. They are separate because the question asked of the picture is different, but the first three stages (capture, detect, track) are shared.

**3. YOLO detection.** `VehicleDetector.detect()` runs YOLO11s at 640 px with confidence 0.35 and keeps only the four COCO vehicle classes. On this machine it runs FP16 on the RTX 4050. Detection answers "where is a vehicle in this frame" and nothing else — it has no idea about bays.

**4. ByteTrack tracking.** The same detector call with `persist=True` carries the `track_id` forward. This matters twice: a parked car must be one object, not a new one every frame, and the *evidence* for a bay must be attributable. The fixture gate clip skips YOLO and replays labelled boxes with ids from `data/fixtures/gate_visit_tracks.json` — which is why health marks it `synthetic_replay: true`.

**5. Geometry.** Each bay is a hand-drawn quadrilateral in normalized coordinates in `configs/slots/lot_a_cam1.json`, produced by `scripts/calibrate.py`. For a vehicle box, the bottom 40 % is taken as its "ground footprint" (the part actually on the tarmac), clipped against the bay polygon with the Sutherland–Hodgman algorithm, and scored as containment (`geometry.py:160`). Containment, not bay coverage, is what decides which bay a car belongs to.

**6. Occupancy engine.** `occupancy_engine.py` turns a set of (track, bay, score) proposals into a one-car-one-bay assignment, greedily from the strongest evidence, and flags a vehicle that sits over a line as a `violation` rather than silently choosing one of two bays.

**7. Slot FSM.** Each bay is a small state machine: `FREE → PENDING_OCCUPIED → OCCUPIED` needs 0.6 s of continuous evidence, and `OCCUPIED → PENDING_FREE → FREE` needs 8.0 s with nothing in it. `UNKNOWN` means "the camera is not telling us" and is set only when a camera goes silent; it is never silently converted to free, and never persisted as a `PENDING_*` state.

**8. Event bus.** `InMemoryEventBus` is the hinge of the design: producers publish and return immediately, and consumers drain at their own speed. The event names in `core/schemas.py:278-290` are the whole contract between the AI layers and the service layers. There is **no** `slot_status_changed` event — the names are `slot_snapshot`, `slot_occupied` and `slot_freed`.

**9. Persistence worker.** A daemon thread takes up to 200 events at a time, writes them in one transaction, and on failure retries that event on the next pass while keeping at most 200 failure samples. It exists only when the server was started with `--persist`; without it the API answers from the database and the socket, but writes nothing.

**10. Database.** SQLite in WAL mode so readers never block the writer, `busy_timeout=5000`, foreign keys on, schema at Alembic `0003`. Nine tables; the two plate tables are empty by configuration, not by failure.

**11. Backend.** FastAPI with 27 paths under `/api/v1`. Authentication is a bearer header, except the camera image routes, which also accept `?token=` because an `<img>` cannot set a header (`deps.viewer_user`), and the sockets, which accept a one-time `?ticket=` and **refuse** the access token (`routers/live_ws.py:117-127`). Every failure becomes the same `{"error":{"code","message"}}` envelope.

**12. Live delivery.** `WebSocketHub` subscribes to the bus inside the process and fans events out to every dashboard; each client has a 200-message queue and drops its own backlog rather than stalling the pipeline. `FrameHub` holds only the newest annotated frame per camera, which the MJPEG generator encodes at 5 fps per viewer.

**13. Dashboard.** React 19 + TypeScript. On mount it seeds the bay list from `GET /slots`, then folds socket messages into it with `applySlotMessage`, and re-derives all counters from the bay list so the cards always match the map. The camera card sets `img src` once per connect (not per render) so a socket burst cannot reopen the stream. `LiveChip` reports the *socket's* state, not the page's.

**14. Deployment.** Nginx is the single same-origin front door: `/api/` rate-limited, `…/stream.mjpg` with buffering disabled, `/ws/` with upgrade headers, `/` serving the built bundle from `frontend/dist`. In Docker, `api` runs `migrate.py upgrade head` then `serve.py --persist`. **Currently this layer is not running** — the project is being used through the Vite dev proxy.

---

## 3.5 Data-flow direction, in one sentence per edge

```
clip → capture → detect → track → geometry → assign → FSM ─┬→ bus ─┬→ ws hub → browser socket
                                                            │        └→ persistence worker → SQLite → REST → browser
                                       annotated frame ─────┴→ FrameHub → MJPEG → browser <img>
```

Nothing writes to SQLite except the persistence worker (plus auth/audit writes and admin configuration calls). The browser never writes to the database directly; it reads REST, listens to the socket, and looks at the picture.
