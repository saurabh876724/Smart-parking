/** The pipeline as the repository has it, one entry per component that exists.
 *
 * `status` is the part an examiner checks: `running` is what this machine is doing
 * while the server is up, `configured` is real code that is switched off, and
 * `planned` is a component the design mentions and the source does not have. The
 * architecture view renders the last group in its own lane, never inline.
 */

export type NodeStatus = 'running' | 'configured' | 'planned'

export interface ArchNode {
  id: string
  title: string
  /** The library or mechanism, in the words the code uses. */
  tech: string
  detail: string
  /** Where to open to check the claim. */
  file: string
  status: NodeStatus
}

export interface ArchStage {
  id: string
  label: string
  caption: string
  nodes: ArchNode[]
}

export const ARCH_STAGES: readonly ArchStage[] = [
  {
    id: 'input',
    label: 'Video input',
    caption: 'Two files on disk, opened as if they were cameras.',
    nodes: [
      {
        id: 'clip-lot',
        title: 'Recorded lot clip',
        tech: 'OpenCV VideoCapture, file source',
        detail: 'Real footage at 478x850, 30 fps authored, replayed on its own clock so the FSM sees plausible timing.',
        file: 'data/samples/lot_shot5.mp4 · video/sources.py:39 open_capture',
        status: 'running',
      },
      {
        id: 'clip-gate',
        title: 'Rendered gate fixture',
        tech: 'file clip + tracks JSON',
        detail: 'A synthetic clip whose labelled boxes are replayed from a JSON file, so YOLO has nothing to find. Every frame it draws says SYNTHETIC.',
        file: 'data/fixtures/gate_visit.mp4 + gate_visit_tracks.json',
        status: 'running',
      },
      {
        id: 'frame-grabber',
        title: 'Capture + replay pacing',
        tech: 'FrameGrabber, pace_replay, loop',
        detail: 'Keeps only the newest frame for a live source and rewinds a clip when it ends, so a decoder faster than the clip does not fake time.',
        file: 'video/grabber.py · pipeline/lot_pipeline.py:138-148',
        status: 'running',
      },
    ],
  },
  {
    id: 'perception',
    label: 'Detection & tracking',
    caption: 'Where is a vehicle in this frame - and is it the same one as last frame.',
    nodes: [
      {
        id: 'yolo',
        title: 'YOLO11s vehicle detection',
        tech: 'ultralytics, imgsz 640, conf 0.35, FP16 on CUDA',
        detail: 'Keeps only car, motorcycle, bus and truck. CPU fallback is implemented and reported by /health when no device is visible.',
        file: 'detection/vehicle_detector.py',
        status: 'running',
      },
      {
        id: 'bytetrack',
        title: 'ByteTrack re-identification',
        tech: 'tracker.update(persist=True)',
        detail: 'Carries one track id across frames so a parked car is one object, and the evidence for a bay is attributable.',
        file: 'tracking/tracker.py',
        status: 'running',
      },
      {
        id: 'anpr',
        title: 'ANPR plate reading',
        tech: 'OpenCV OCR pipeline, fully implemented and tested',
        detail: 'Switched off on every served stream (read_plates: false), so no plate_read event is emitted and the two plate tables stay empty by configuration.',
        file: 'anpr/pipeline.py · configs/app.yaml anpr.enabled',
        status: 'configured',
      },
    ],
  },
  {
    id: 'state',
    label: 'Geometry & state',
    caption: 'Which bay does this vehicle belong to, and is that claim old enough to commit.',
    nodes: [
      {
        id: 'geometry',
        title: 'Bay geometry',
        tech: 'Sutherland-Hodgman clipping, containment score',
        detail: 'Hand-drawn quadrilaterals in normalised coordinates from the calibration tool; the bottom 40 % of a box is its ground footprint.',
        file: 'configs/slots/lot_a_cam1.json · parking/geometry.py:160',
        status: 'running',
      },
      {
        id: 'occupancy',
        title: 'Occupancy engine',
        tech: 'greedy one-car-one-bay assignment',
        detail: 'Claim needs containment >= 0.45; a vehicle over a line at 0.20 or more on two bays publishes a violation instead of picking one; below 0.20 everywhere is the aisle.',
        file: 'parking/occupancy_engine.py',
        status: 'running',
      },
      {
        id: 'fsm',
        title: 'Per-bay finite state machine',
        tech: 'FREE / PENDING_OCCUPIED / OCCUPIED / PENDING_FREE / UNKNOWN',
        detail: '0.6 s of continuous evidence to occupy, 8.0 s of nothing to free. The asymmetry is deliberate: a false FREE double-books a bay. PENDING states never leave memory.',
        file: 'parking/slot_fsm.py · configs/app.yaml occupancy',
        status: 'running',
      },
      {
        id: 'bus',
        title: 'In-memory event bus',
        tech: 'InMemoryEventBus, bounded queue',
        detail: 'Producers publish and return; consumers drain at their own speed. slot_snapshot, slot_occupied, slot_freed, summary, vehicle_entered, vehicle_exited, plate_read, violation.',
        file: 'core/events.py · core/schemas.py:278-290',
        status: 'running',
      },
    ],
  },
  {
    id: 'delivery',
    label: 'Live delivery',
    caption: 'The same frames, two ways: a picture for the eye and events for the numbers.',
    nodes: [
      {
        id: 'render',
        title: 'Frame annotation',
        tech: 'OpenCV draw: boxes, track ids, polygons, HUD',
        detail: 'The identical drawing the pipeline writes into its annotated clips, so the dashboard shows what the detector worked on.',
        file: 'parking/render.py',
        status: 'running',
      },
      {
        id: 'framehub',
        title: 'FrameHub + MJPEG',
        tech: 'latest frame per camera, multipart x-mixed-replace',
        detail: 'Holds one frame per camera and encodes at 5 fps per viewer; 409 no_frame until a pipeline has read one.',
        file: 'api/live.py · routers/cameras.py:232',
        status: 'running',
      },
      {
        id: 'wshub',
        title: 'WebSocketHub',
        tech: '/ws/live, per-client queue of 200',
        detail: 'Maps an event type to a channel and fans out. A client that cannot keep up drops its own backlog and increments dropped_events rather than stalling the pipeline.',
        file: 'api/live.py:168-234 · routers/live_ws.py',
        status: 'running',
      },
    ],
  },
  {
    id: 'storage',
    label: 'Persistence',
    caption: 'Only a worker thread writes, and only when the server was told to.',
    nodes: [
      {
        id: 'persist',
        title: 'Persistence worker',
        tech: 'daemon thread, batch 200, per-event retry',
        detail: 'Runs only when serve.py was started with --persist; skips the per-frame summary rows so history is not one row per frame.',
        file: 'services/persistence.py · db/repositories/event_log.py:34',
        status: 'running',
      },
      {
        id: 'sqlite',
        title: 'SQLite in WAL mode',
        tech: 'SQLAlchemy 2, Alembic 0003, busy_timeout 5000',
        detail: 'Nine tables. Readers never block the writer. A second writer to the same file is refused at startup by a guard in serve.py.',
        file: 'db/session.py · data/smart_parking.db',
        status: 'running',
      },
      {
        id: 'postgres',
        title: 'PostgreSQL profile',
        tech: 'compose profile postgres, declared not running',
        detail: 'The engine factory supports a URL, and the compose file declares the service under a profile; the container is not part of this deployment.',
        file: 'deploy/docker-compose.yml',
        status: 'planned',
      },
    ],
  },
  {
    id: 'service',
    label: 'API service',
    caption: 'One FastAPI process, one envelope for every failure.',
    nodes: [
      {
        id: 'fastapi',
        title: 'FastAPI under /api/v1',
        tech: '27 paths: auth, cameras, slots, sessions, plates, stats, health, audit',
        detail: 'Bearer auth except the image routes (?token=) and the sockets (one-time ?ticket=, token refused). Errors are {error:{code,message}}.',
        file: 'api/app.py:197-200 · api/deps.py',
        status: 'running',
      },
      {
        id: 'auth',
        title: 'JWT auth, rotation and throttle',
        tech: 'access 900 s, refresh 43 200 s rotating, 10 failures / 300 s',
        detail: 'Revocation is a timestamp per account, so logout withdraws both credentials rather than only stopping their use.',
        file: 'api/security.py · api/throttle.py',
        status: 'running',
      },
    ],
  },
  {
    id: 'client',
    label: 'This dashboard',
    caption: 'React 19 + TypeScript, TanStack Query, one socket per tab.',
    nodes: [
      {
        id: 'react',
        title: 'React 19 + Vite + Tailwind v4',
        tech: 'TanStack Query, recharts, lucide',
        detail: 'Seeds bays from GET /slots, folds /ws/live messages into the same list, and re-derives every count from it so the cards cannot disagree with the map.',
        file: 'frontend/src/App.tsx · lib/live.tsx · lib/slots.ts',
        status: 'running',
      },
      {
        id: 'session',
        title: 'Session lifecycle',
        tech: 'single-flight refresh, SESSION_END reasons',
        detail: 'An expired access token is refreshed once and the call retried; a revoked or wrong-use credential ends the session and says which.',
        file: 'frontend/src/lib/api.ts · lib/auth.tsx',
        status: 'running',
      },
      {
        id: 'nginx',
        title: 'Nginx same-origin front door',
        tech: '/api/ rate-limited, stream.mjpg buffering off, /ws/ upgrade',
        detail: 'Written and verified as a configuration, but this deployment reaches the API through the Vite dev proxy rather than nginx in a container.',
        file: 'deploy/nginx-site.conf',
        status: 'configured',
      },
    ],
  },
]

/** Everything the design mentions that this repository does not do yet. */
export const PLANNED_COMPONENTS: readonly ArchNode[] = [
  {
    id: 'physical-camera',
    title: 'Physical camera input (RTSP or webcam)',
    tech: 'code path exists, nothing configured',
    detail: 'open_capture accepts an index or a URL, but every source in this deployment is a file, so no FPS figure describes a field camera.',
    file: 'video/sources.py:39',
    status: 'planned',
  },
  {
    id: 'billing',
    title: 'Billing and revenue',
    tech: 'no tariff or payment model',
    detail: 'The endpoint refuses with 501 rather than answering an empty series, because an empty series would read as zero revenue.',
    file: 'api/routers/stats.py:93',
    status: 'planned',
  },
  {
    id: 'lots-table',
    title: 'parking_lots table',
    tech: 'camera_id is the lot key for now',
    detail: 'A lot is identified by its camera because a table holding nothing but a name would make camera_id mean two things.',
    file: 'api/routers/slots.py:120',
    status: 'planned',
  },
  {
    id: 'docker-stack',
    title: 'Docker api / nginx / frontend services',
    tech: 'images described, containers absent',
    detail: 'Only the database container has ever been run on this machine; the API and the bundle are served by the dev processes.',
    file: 'deploy/docker-compose.yml',
    status: 'planned',
  },
]

export const STATUS_LABEL: Record<NodeStatus, string> = {
  running: 'Running here',
  configured: 'In the code, switched off',
  planned: 'Not implemented',
}
