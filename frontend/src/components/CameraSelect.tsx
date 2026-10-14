import type { CameraInfo } from '../lib/api'

/** The camera the page is looking at, chosen from the server's own registry.
 *
 * Only cameras `GET /cameras` returns are offered, so a demo cannot click its way
 * into a lot that was never configured; the ones the pipeline is not running are
 * still selectable, because reading their committed bays is honest work - the
 * labels just say what is and is not being watched.
 */
export function CameraSelect({
  cameras,
  value,
  onChange,
}: {
  cameras: CameraInfo[]
  value: string
  onChange: (id: string) => void
}) {
  const options: CameraInfo[] = cameras.length > 0 ? cameras : [{
    camera_id: value,
    name: value,
    role: 'LOT',
    source: null,
    slots_file: null,
    gate_file: null,
    fps_limit: 0,
    enabled: false,
    last_seen_at: null,
    live: {
      camera_id: value,
      streaming: false,
      frames_available: false,
      role: 'UNKNOWN',
      synthetic_replay: false,
      read_plates: false,
      loop: false,
      passes: 0,
      frames_processed: null,
      error: null,
    },
  }]

  return (
    <label className="flex items-center gap-2">
      <span className="text-[0.7rem] uppercase tracking-[0.12em] text-mist-600 font-semibold">Camera</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="field px-2.5 py-1.5 text-sm min-w-[12rem] max-w-full"
      >
        {options.map((camera) => (
          <option key={camera.camera_id} value={camera.camera_id} className="bg-ink-900">
            {camera.name || camera.camera_id} · {camera.role} · {stateOf(camera)}
          </option>
        ))}
      </select>
    </label>
  )
}

function stateOf(camera: CameraInfo): string {
  if (camera.live.streaming) return 'streaming'
  if (camera.live.frames_available) return 'idle, frames held'
  if (!camera.enabled) return 'disabled'
  return 'not running here'
}
