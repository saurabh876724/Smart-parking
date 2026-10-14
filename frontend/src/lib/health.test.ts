import { describe, expect, it } from 'vitest'
import type { CameraInfo, CameraLive, HealthCheck } from './api'
import {
  anprRow,
  apiRow,
  backendRows,
  cameraRows,
  clientRows,
  databaseRow,
  eventsRow,
  gpuRow,
  pipelineRow,
  STATE_LABEL,
} from './health'

function live(overrides: Partial<CameraLive> = {}): CameraLive {
  return {
    camera_id: 'lot_cam1',
    streaming: true,
    frames_available: true,
    role: 'lot',
    synthetic_replay: false,
    read_plates: false,
    loop: true,
    passes: 1,
    frames_processed: 1412,
    error: null,
    ...overrides,
  }
}

function healthFixture(overrides: Partial<HealthCheck> = {}): HealthCheck {
  return {
    status: 'ok',
    database: 'ok',
    anpr_enabled: false,
    gpu: { available: true, device: 'cuda:0', torch_version: '2.8.0', memory_total_mib: 6141, memory_free_mib: 4096 },
    cameras: [live()],
    ws_clients: 1,
    frames_in: 1412,
    events_out: 845,
    dropped_events: 0,
    ws_ticket_refusals: 0,
    uptime_s: 89.31,
    ...overrides,
  }
}

function camera(overrides: Partial<CameraInfo> = {}): CameraInfo {
  return {
    camera_id: 'lot_cam1',
    name: 'Lot camera 1',
    role: 'lot',
    source: 'data/samples/lot_shot5.mp4',
    slots_file: 'configs/slots/lot_a_cam1.json',
    gate_file: null,
    fps_limit: 5,
    enabled: true,
    last_seen_at: '2026-10-14T09:00:00',
    live: live(),
    ...overrides,
  }
}

describe('databaseRow', () => {
  it('accepts the server wording for a healthy database', () => {
    expect(databaseRow(healthFixture()).state).toBe('ok')
  })

  it('treats pending migrations as a failure, not a warning', () => {
    const row = databaseRow(healthFixture({ database: 'migrations pending: 0004_add_lots' }))
    expect(row.state).toBe('bad')
    expect(row.detail).toContain('0004_add_lots')
  })

  it('treats an unreachable database as a failure and quotes why', () => {
    const row = databaseRow(healthFixture({ database: 'unreachable: database is locked' }))
    expect(row.state).toBe('bad')
    expect(row.value).toBe('Unreachable')
  })

  it('says unknown before health has answered', () => {
    expect(databaseRow(null).state).toBe('unknown')
  })
})

describe('apiRow', () => {
  it('separates this browser failing from the server being degraded', () => {
    const failed = apiRow({ health: null, failed: true, errorText: 'fetch failed' })
    expect(failed.state).toBe('bad')
    expect(failed.detail).toBe('fetch failed')

    const degraded = apiRow({ health: healthFixture({ status: 'degraded', database: 'unreachable: x' }), failed: false })
    expect(degraded.state).toBe('warn')
    expect(degraded.value).toContain('database side')
  })

  it('does not call an open request a failure', () => {
    expect(apiRow({ health: null, failed: false }).state).toBe('unknown')
  })
})

describe('pipelineRow', () => {
  it('counts the cameras this process is actually driving', () => {
    const row = pipelineRow(healthFixture({ cameras: [live(), live({ camera_id: 'gate_cam1', streaming: false, frames_available: false })] }))
    expect(row.state).toBe('ok')
    expect(row.value).toBe('1/2 producing frames')
  })

  it('reports a camera error rather than hiding it behind a healthy status', () => {
    const row = pipelineRow(healthFixture({ status: 'degraded', cameras: [live({ error: 'source could not be opened' })] }))
    expect(row.state).toBe('bad')
    expect(row.detail).toContain('source could not be opened')
  })

  it('is honest about a process that started no pipeline at all', () => {
    const row = pipelineRow(healthFixture({ cameras: [] }))
    expect(row.state).toBe('warn')
    expect(row.value).toBe('Not started in this process')
  })
})

describe('eventsRow', () => {
  it('passes a clean delivery and flags a dropped event', () => {
    expect(eventsRow(healthFixture()).state).toBe('ok')
    expect(eventsRow(healthFixture()).detail).toContain('845 events published')
    const dropped = eventsRow(healthFixture({ dropped_events: 3 }))
    expect(dropped.state).toBe('warn')
    expect(dropped.value).toBe('3 dropped')
  })
})

describe('gpuRow and anprRow', () => {
  it('reports a CPU fallback as something to watch, with the server reason', () => {
    const row = gpuRow(healthFixture({ gpu: { available: false, reason: 'torch.cuda.is_available() is False' } }))
    expect(row.state).toBe('warn')
    expect(row.detail).toBe('torch.cuda.is_available() is False')
  })

  it('keeps ANPR off as a configuration fact, not a fault', () => {
    expect(anprRow(healthFixture()).state).toBe('warn')
    expect(anprRow(healthFixture()).value).toBe('Disabled in config')
    expect(anprRow(healthFixture({ anpr_enabled: true })).state).toBe('ok')
  })
})

describe('backendRows', () => {
  it('answers the whole page in a stable order and never mixes in browser state', () => {
    expect(backendRows({ health: healthFixture(), failed: false }).map((row) => row.id)).toEqual([
      'api',
      'db',
      'pipeline',
      'events',
      'gpu',
      'anpr',
    ])
    for (const row of backendRows({ health: healthFixture(), failed: false })) {
      expect(STATE_LABEL[row.state]).toBeTruthy()
    }
  })
})

describe('clientRows', () => {
  it('keeps a dropped socket apart from a healthy server', () => {
    const rows = clientRows({ socket: 'offline', apiOk: true, close: { code: 4401, reason: 'ticket refused' } })
    const rest = rows.find((row) => row.id === 'rest')
    const socket = rows.find((row) => row.id === 'socket')
    expect(rest?.state).toBe('ok')
    expect(socket?.state).toBe('bad')
    expect(socket?.detail).toContain('code 4401')
  })

  it('keeps a refused REST read apart from a broken database', () => {
    const rows = clientRows({ socket: 'live', apiOk: false })
    expect(rows.find((row) => row.id === 'rest')?.state).toBe('bad')
    expect(rows.find((row) => row.id === 'socket')?.state).toBe('ok')
  })

  it('has a state for a channel that has not been opened yet', () => {
    expect(clientRows({ socket: 'idle', apiOk: null }).find((row) => row.id === 'socket')?.state).toBe('unknown')
  })
})

describe('cameraRows', () => {
  it('labels a replayed fixture as one and a configured camera as unstarted', () => {
    const rows = cameraRows(
      camera({
        camera_id: 'gate_cam1',
        live: live({ camera_id: 'gate_cam1', synthetic_replay: true, streaming: false, frames_available: true, passes: 2 }),
      }),
    )
    const stream = rows.find((row) => row.id === 'gate_cam1:stream')
    expect(stream?.detail).toContain('not a live field camera')
    expect(stream?.value).toContain('Idle · 1412 frame(s) over 2 pass(es)')
  })

  it('shows that a row can be configured and yet not running in this process', () => {
    const rows = cameraRows(camera({ live: live({ streaming: false, frames_available: false, frames_processed: null }) }))
    expect(rows.find((row) => row.id === 'lot_cam1:source')?.state).toBe('ok')
    expect(rows.find((row) => row.id === 'lot_cam1:stream')?.state).toBe('warn')
    expect(rows.find((row) => row.id === 'lot_cam1:stream')?.value).toBe('Not running in this process')
  })

  it('marks a disabled camera and a reader error differently', () => {
    expect(cameraRows(camera({ enabled: false, live: live({ streaming: false, frames_available: false }) }))
      .find((row) => row.id === 'lot_cam1:source')?.value).toBe('Disabled')
    expect(cameraRows(camera({ live: live({ error: 'no such file' }) }))
      .find((row) => row.id === 'lot_cam1:source')?.state).toBe('bad')
  })
})
