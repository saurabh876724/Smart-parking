import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Activity, ArrowLeft, ArrowRight, Car, HelpCircle, Maximize, ParkingSquare, Presentation } from 'lucide-react'
import { useLive } from '../../lib/live'
import { ARCH_STAGES, PLANNED_COMPONENTS } from '../../lib/architecture'
import { FLOW_STEPS } from '../../lib/flow'
import { occupancyPercent, formatPercent } from '../../lib/stats'
import type { ViewId } from '../../lib/nav'
import { CameraPreview } from '../CameraPreview'
import { Chip } from '../ui/chip'
import { StatCard } from '../ui/stat-card'

/** A five-minute walkthrough, built out of the same modules the other pages use.
 *
 * Slide content is derived, not typed twice: the architecture chain comes from
 * `lib/architecture.ts` and the nine steps from `lib/flow.ts`, so a slide cannot
 * claim a component the repository does not have.
 */

interface Slide {
  id: string
  kicker: string
  title: string
  /** Roughly what this slide takes to talk through, in the presenter's own order. */
  say?: string
  body: ReactNode
}

function Bullet({ children }: { children: ReactNode }) {
  return (
    <li className="flex gap-3 text-base sm:text-lg leading-relaxed text-mist-200">
      <span className="mt-2.5 h-1.5 w-1.5 shrink-0 rounded-full bg-volt-400" />
      <span className="min-w-0">{children}</span>
    </li>
  )
}

function Metric({ value, label }: { value: ReactNode; label: string }) {
  return (
    <div className="rounded-xl border border-line bg-ink-900/70 px-4 py-3.5">
      <p className="text-2xl sm:text-3xl font-semibold text-mist-50 tabular leading-none">{value}</p>
      <p className="text-xs text-mist-400 mt-2 leading-snug">{label}</p>
    </div>
  )
}

export function ExamView({ navigate }: { navigate: (id: ViewId) => void }) {
  const live = useLive()
  const camera = live.cameras.find((entry) => entry.camera_id === live.cameraId)
  const percent = formatPercent(occupancyPercent(live.counts))

  const slides: Slide[] = useMemo(
    () => [
      {
        id: 'intro',
        kicker: '1 · Introduction',
        title: 'SmartPark AI — occupancy from ordinary CCTV',
        say: 'Name the system, the input, and the one output that matters.',
        body: (
          <div className="space-y-6">
            <ul className="space-y-3">
              <Bullet>Video from a parking-lot camera goes in; a committed free / occupied state for every marked bay comes out.</Bullet>
              <Bullet>YOLO11s finds vehicles, ByteTrack keeps one identity per vehicle, bay polygons decide which bay a vehicle belongs to, and a state machine decides when that is worth calling a fact.</Bullet>
              <Bullet>The result is live on a dashboard: counts, a layout map, the processed picture, session history, and a health page.</Bullet>
            </ul>
            <div className="grid gap-3 sm:grid-cols-3">
              <Metric value="2" label="pipeline loops: one for a lot view, one for an entrance view" />
              <Metric value="5" label="bay states the machine holds; two of them never leave memory" />
              <Metric value="0" label="hardcoded numbers on any screen - every value is read from the API" />
            </div>
          </div>
        ),
      },
      {
        id: 'problem',
        kicker: '2 · Problem and objectives',
        title: 'Counting bays without sensors in them',
        say: 'Why a camera instead of magnetic loops, and what "correct" means here.',
        body: (
          <div className="space-y-5">
            <ul className="space-y-3">
              <Bullet>Per-bay sensors cost installation and calibration per slot; a camera already exists in most lots.</Bullet>
              <Bullet>The hard part is not seeing a car. It is deciding, from one frame at a time, when a bay has genuinely changed state.</Bullet>
              <Bullet>Objectives set for this project: a stable bay state, an attributable live picture, a persisted history, and honest reporting of anything the system cannot do.</Bullet>
            </ul>
            <div className="rounded-xl border border-volt-500/30 bg-volt-500/8 p-4">
              <p className="text-sm sm:text-base text-mist-100 leading-relaxed">
                The design rule that shapes everything else: <span className="font-semibold text-volt-200">an unreported bay is not an empty bay.</span>{' '}
                UNKNOWN is its own state, its own card, and is never added to available capacity.
              </p>
            </div>
          </div>
        ),
      },
      {
        id: 'stack',
        kicker: '3 · Tech stack',
        title: 'Python for the eyes, FastAPI for the doors, React for the wall',
        body: (
          <div className="grid gap-3 sm:grid-cols-2">
            {[
              ['Detection and tracking', 'ultralytics YOLO11s at 640 px, conf 0.35, FP16 on an RTX 4050; ByteTrack with persist=True for stable ids'],
              ['Geometry and state', 'Sutherland-Hodgman polygon clipping for containment; a five-state FSM per bay with 0.6 s / 8.0 s dwell'],
              ['Service', 'FastAPI with 27 routes under /api/v1, JWT auth, rotating refresh, one-time WebSocket tickets, failed-login throttling'],
              ['Storage', 'SQLAlchemy 2 + Alembic on SQLite in WAL mode, nine tables; a Postgres profile is declared and not run'],
              ['Live delivery', 'in-memory bounded event bus; per-client WebSocket queue of 200; MJPEG from the newest annotated frame'],
              ['Dashboard', 'React 19 + TypeScript + Vite, Tailwind v4 tokens, TanStack Query, recharts, vitest'],
            ].map(([title, detail]) => (
              <div key={title} className="rounded-xl border border-line bg-ink-900/70 p-4">
                <p className="text-sm font-semibold text-mist-50">{title}</p>
                <p className="text-xs sm:text-sm text-mist-400 leading-relaxed mt-1.5">{detail}</p>
              </div>
            ))}
          </div>
        ),
      },
      {
        id: 'architecture',
        kicker: '4 · Architecture',
        title: 'Seven stages, in the order the data moves',
        say: 'Say the chain once, then name the one place the design is unusual: the bus.',
        body: (
          <div className="space-y-5">
            <div className="grid gap-2.5">
              {ARCH_STAGES.map((stage, index) => (
                <div key={stage.id} className="flex items-start gap-3">
                  <span className="mt-0.5 shrink-0 inline-flex h-7 w-7 items-center justify-center rounded-full border border-volt-500/40 bg-volt-500/12 text-xs font-semibold text-volt-300 tabular">
                    {index + 1}
                  </span>
                  <div className="min-w-0 border-b border-line/70 pb-2.5 flex-1">
                    <p className="text-base sm:text-lg font-semibold text-mist-50">{stage.label}</p>
                    <p className="text-xs sm:text-sm text-mist-400">{stage.nodes.map((node) => node.title).join(' · ')}</p>
                  </div>
                </div>
              ))}
            </div>
            <ul className="space-y-2.5">
              <Bullet>The event bus is the hinge: producers publish and return immediately, so the pipeline never waits on a browser or a disk write.</Bullet>
              <Bullet>Two consumers take from it at their own speed - the WebSocket hub and one persistence thread. Only that thread writes history.</Bullet>
            </ul>
          </div>
        ),
      },
      {
        id: 'flow',
        kicker: '5 · Working flow',
        title: 'One frame, nine steps',
        say: 'Read the list, then linger on the two branches an examiner asks about: no detection, and a dead camera.',
        body: (
          <div className="space-y-4">
            <ol className="grid gap-2">
              {FLOW_STEPS.map((step) => (
                <li key={step.id} className="flex items-baseline gap-3 border-b border-line/60 pb-2">
                  <span className="text-xs font-semibold text-volt-300 tabular shrink-0 w-5">{step.n}</span>
                  <span className="text-sm sm:text-base text-mist-100 font-medium shrink-0">{step.title}</span>
                  <span className="text-xs sm:text-sm text-mist-400 leading-relaxed min-w-0">
                    {step.branches.length} branch{step.branches.length === 1 ? '' : 'es'} · {step.file.split('·')[0].trim()}
                  </span>
                </li>
              ))}
            </ol>
            <div className="grid gap-3 sm:grid-cols-4">
              <Metric value="0.6 s" label="continuous evidence before a bay becomes OCCUPIED" />
              <Metric value="8.0 s" label="of nothing before it becomes FREE - slow on purpose" />
              <Metric value="0.45" label="containment of the vehicle footprint needed to claim a bay" />
              <Metric value="UNKNOWN" label="what a silent camera sets, never a guessed FREE" />
            </div>
          </div>
        ),
      },
      {
        id: 'live',
        kicker: '6 · Live system',
        title: 'What the pipeline reports right now',
        say: 'Point at the picture, then at the counts, and let the socket change a bay while the room watches.',
        body: (
          <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <StatCard label="Occupied" value={live.counts.occupied} tone="busy" icon={<Car className="w-4 h-4" />} hint="committed OCCUPIED bays" loading={live.loading} />
              <StatCard label="Available" value={live.counts.available} tone="good" icon={<Activity className="w-4 h-4" />} hint="committed FREE bays" loading={live.loading} />
              <StatCard label="Not reported" value={live.counts.unknown} tone="unknown" icon={<HelpCircle className="w-4 h-4" />} hint="no committed state - not available capacity" loading={live.loading} />
              <StatCard label="Occupancy" value={percent} tone="info" icon={<ParkingSquare className="w-4 h-4" />} hint="of reported bays only; UNKNOWN is excluded from the denominator" />
            </div>
            <CameraPreview cameraId={live.cameraId} live={camera?.live} />
            <p className="text-xs text-mist-600 leading-relaxed">
              The boxes, bay outlines and the HUD in that picture are drawn by the same backend code that writes the
              statuses above (<code>parking/render.py</code>), and the stream is authenticated with a token that an
              image element can carry. This tab is reading the real process: the totals above are counts of rows{' '}
              <code>GET /slots</code> returned, and the chip in the header is this socket's state.
            </p>
          </div>
        ),
      },
      {
        id: 'testing',
        kicker: '7 · Verified results',
        title: 'Measured, not asserted',
        say: 'Give the numbers and where each was measured; then name the coverage gap before anyone else does.',
        body: (
          <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Metric value="646" label="backend pytest tests, 85.38 s, exit 0 - docs/project_report.md" />
              <Metric value="105" label="frontend vitest logic and contract tests, 7 files, exit 0" />
              <Metric value="0.600 s" label="evidence-to-OCCUPIED, from the timestamps of two socket events" />
              <Metric value="845 / 0" label="socket messages relayed in 45 s / events dropped on the floor" />
            </div>
            <ul className="space-y-2.5">
              <Bullet>1,412 frames of real footage processed in 89.31 s of uptime - about 16 fps of a 30 fps clip, on a laptop GPU, with the replay clock keeping the state machine honest.</Bullet>
              <Bullet>Track ids held stable across three overlay samples spanning 2.7 s while the box geometry moved, which is what makes bay evidence attributable.</Bullet>
              <Bullet>MJPEG delivered 37 distinct frames in about 8 s against a configured 5 fps, so the picture is limited by the endpoint on purpose.</Bullet>
              <Bullet>Honest gap: vitest runs in the node environment without jsdom, so the frontend tests cover logic and contracts, not component rendering. The visual verification of these pages was done by loading them in a browser.</Bullet>
            </ul>
          </div>
        ),
      },
      {
        id: 'limits',
        kicker: '8 · Limitations and future scope',
        title: 'What this system does not do',
        say: 'Read these out; they are the difference between a demo and a claim.',
        body: (
          <div className="space-y-4">
            <ul className="space-y-3">
              {PLANNED_COMPONENTS.map((node) => (
                <Bullet key={node.id}>
                  <span className="font-semibold text-mist-50">{node.title}.</span> {node.detail}
                </Bullet>
              ))}
              <Bullet>
                <span className="font-semibold text-mist-50">Plates are not read on the served streams.</span> The ANPR
                pipeline is implemented and tested, but <code>read_plates</code> is false on every stream, so no
                plate_read event is emitted and the two plate tables stay empty by configuration.
              </Bullet>
              <Bullet>
                <span className="font-semibold text-mist-50">Every figure here describes a decoder and a GPU, not a field
                installation,</span> because every input is a file on disk.
              </Bullet>
            </ul>
            <div className="rounded-xl border border-line bg-ink-950/60 p-4">
              <p className="text-sm font-semibold text-mist-50 mb-2">Next, in the order that would pay for itself</p>
              <ul className="text-xs sm:text-sm text-mist-400 space-y-1.5 list-disc pl-4">
                <li>Point one real RTSP camera at one real lot and re-measure everything.</li>
                <li>Turn ANPR on for the gate stream and let sessions carry a plate.</li>
                <li>A lot entity above the camera, so one lot can be watched from several angles.</li>
                <li>Tariffs and billing on top of the session history that already exists.</li>
              </ul>
            </div>
          </div>
        ),
      },
    ],
    [live.cameraId, live.counts, live.loading, percent, camera?.live],
  )

  const [index, setIndex] = useState(0)
  const go = useCallback(
    (next: number) => setIndex(Math.max(0, Math.min(slides.length - 1, next))),
    [slides.length],
  )

  // Keyboard first: a presenter with a clicker sends arrow keys, and the room should
  // not have to watch anyone reaching for a mouse.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target as HTMLElement | null
      if (target && /^(input|textarea|select)$/i.test(target.tagName)) return
      if (event.key === 'ArrowRight' || event.key === 'PageDown' || event.key === ' ') {
        event.preventDefault()
        go(index + 1)
      } else if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
        event.preventDefault()
        go(index - 1)
      } else if (event.key === 'Home') {
        go(0)
      } else if (event.key === 'End') {
        go(slides.length - 1)
      } else if (event.key === 'Escape') {
        navigate('overview')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go, index, navigate, slides.length])

  const slide = slides[index]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Presentation className="w-5 h-5 text-volt-400" />
          <div>
            <p className="text-[0.68rem] uppercase tracking-[0.16em] text-mist-600">Presentation mode</p>
            <h1 className="text-lg font-semibold text-mist-50 tracking-tight">SmartPark AI · five-minute walkthrough</h1>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Chip tone="info" label={`Slide ${index + 1} of ${slides.length}`} />
          <button
            type="button"
            onClick={() => navigate('overview')}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong bg-ink-850 px-3 py-1.5 text-xs text-mist-200 hover:border-volt-500/50 hover:text-mist-50 transition-colors"
            title="Leave the deck; the dashboard stays one click away"
          >
            <Maximize className="w-3.5 h-3.5" />
            Back to dashboard
          </button>
        </div>
      </div>

      <div className="rounded-2xl border border-line bg-ink-900/60 p-6 sm:p-8 min-h-[58vh]">
        <p className="text-[0.7rem] uppercase tracking-[0.18em] text-volt-300">{slide.kicker}</p>
        <h2 className="text-2xl sm:text-3xl font-semibold text-mist-50 tracking-tight mt-2 mb-5">{slide.title}</h2>
        {slide.body}
        {slide.say && (
          <p className="text-xs text-mist-600 mt-6 pt-4 border-t border-line italic">Presenter note — {slide.say}</p>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => go(index - 1)}
            disabled={index === 0}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong bg-ink-850 px-3.5 py-2 text-sm text-mist-200 disabled:opacity-40 disabled:cursor-not-allowed hover:border-volt-500/50 transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            Previous
          </button>
          <button
            type="button"
            onClick={() => go(index + 1)}
            disabled={index === slides.length - 1}
            className="inline-flex items-center gap-1.5 rounded-lg border border-volt-500/50 bg-volt-500/15 px-3.5 py-2 text-sm text-volt-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-volt-500/25 transition-colors"
          >
            Next
            <ArrowRight className="w-4 h-4" />
          </button>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          {slides.map((entry, entryIndex) => (
            <button
              key={entry.id}
              type="button"
              onClick={() => go(entryIndex)}
              aria-label={entry.kicker}
              className={`h-2 rounded-full transition-all ${
                entryIndex === index ? 'w-7 bg-volt-400' : 'w-2 bg-line-strong hover:bg-mist-600'
              }`}
            />
          ))}
        </div>
        <p className="text-xs text-mist-600">
          Arrow keys and space move through the deck; Esc returns to the dashboard.
        </p>
      </div>
    </div>
  )
}
