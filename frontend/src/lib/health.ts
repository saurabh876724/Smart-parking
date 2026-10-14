/** Deriving a health page from `GET /health` and from this browser's own sockets.
 *
 * The split matters more than the rows: `/health` is what the API process can
 * prove about itself, and the WebSocket state is what *this tab* managed to open.
 * A wall display on a bad network looks healthy by the first measure and broken by
 * the second, so the two never share a group here.
 */

import type { CameraInfo, CameraLive, HealthCheck } from './api'
import type { SocketStatus } from './websocket'

export type CheckState = 'ok' | 'warn' | 'bad' | 'unknown'

export interface CheckRow {
  id: string
  label: string
  state: CheckState
  /** The short answer, in the row's value column. */
  value: string
  /** Why - the server's own wording where it has one, so nothing is paraphrased. */
  detail: string
}

export interface BackendSubject {
  /** null while the request is in flight; `failed` once it has been refused. */
  health: HealthCheck | null
  failed: boolean
  errorText?: string
}

export const STATE_LABEL: Record<CheckState, string> = {
  ok: 'Healthy',
  warn: 'Watch',
  bad: 'Failed',
  unknown: 'Unknown',
}

export function databaseRow(h: HealthCheck | null): CheckRow {
  if (!h) return { id: 'db', label: 'Database', state: 'unknown', value: 'No answer', detail: 'Health has not been read.' }
  const text = h.database ?? ''
  if (text === 'ok') return { id: 'db', label: 'Database', state: 'ok', value: 'Reachable, migrated', detail: 'Every migrated table the check names is present.' }
  if (text.startsWith('migrations pending')) {
    return { id: 'db', label: 'Database', state: 'bad', value: 'Migrations pending', detail: text }
  }
  if (text.startsWith('unreachable')) {
    return { id: 'db', label: 'Database', state: 'bad', value: 'Unreachable', detail: text }
  }
  return { id: 'db', label: 'Database', state: 'warn', value: text, detail: 'The backend reported this about its database.' }
}

export function apiRow(subject: BackendSubject): CheckRow {
  if (subject.failed) {
    return {
      id: 'api',
      label: 'API process',
      state: 'bad',
      value: 'Not answering',
      detail: subject.errorText ?? 'This browser could not read /health.',
    }
  }
  const h = subject.health
  if (!h) return { id: 'api', label: 'API process', state: 'unknown', value: 'Reading', detail: 'The health request is still open.' }
  return {
    id: 'api',
    label: 'API process',
    state: h.status === 'ok' ? 'ok' : 'warn',
    value: h.status === 'ok' ? 'Up' : `Up, degraded (${h.database.startsWith('ok') ? 'camera side' : 'database side'})`,
    detail: `Answering for ${Math.round(h.uptime_s)} s of uptime; ${h.ws_clients} browser(s) connected to the event channel.`,
  }
}

export function gpuRow(h: HealthCheck | null): CheckRow {
  if (!h) return { id: 'gpu', label: 'GPU', state: 'unknown', value: 'No answer', detail: 'Health has not been read.' }
  const gpu = h.gpu ?? { available: false }
  if (gpu.available) {
    const memory =
      gpu.memory_total_mib !== undefined
        ? ` · ${gpu.memory_free_mib ?? 0}/${gpu.memory_total_mib} MiB free`
        : ''
    return {
      id: 'gpu',
      label: 'GPU',
      state: 'ok',
      value: gpu.device ?? 'CUDA device visible',
      detail: `torch ${gpu.torch_version ?? '?'}${gpu.cuda_version ? `, CUDA ${gpu.cuda_version}` : ''}${memory}`,
    }
  }
  return {
    id: 'gpu',
    label: 'GPU',
    state: 'warn',
    value: 'No CUDA device',
    detail: gpu.reason ?? 'torch reported no usable device.',
  }
}

export function anprRow(h: HealthCheck | null): CheckRow {
  if (!h) return { id: 'anpr', label: 'ANPR / plate reading', state: 'unknown', value: 'No answer', detail: 'Health has not been read.' }
  return h.anpr_enabled
    ? { id: 'anpr', label: 'ANPR / plate reading', state: 'ok', value: 'Enabled in config', detail: 'The reader is switched on; whether a stream uses it is per camera.' }
    : {
        id: 'anpr',
        label: 'ANPR / plate reading',
        state: 'warn',
        value: 'Disabled in config',
        detail: 'Sessions are matched by track id, not by plate, while this is off.',
      }
}

export function pipelineRow(h: HealthCheck | null): CheckRow {
  if (!h) return { id: 'pipeline', label: 'Pipeline', state: 'unknown', value: 'No answer', detail: 'Health has not been read.' }
  const running = h.cameras.filter((c) => c.streaming || c.frames_available)
  const errored = h.cameras.filter((c) => c.error)
  if (errored.length > 0) {
    return { id: 'pipeline', label: 'Pipeline', state: 'bad', value: `${errored.length} camera(s) errored`, detail: errored.map((c) => `${c.camera_id}: ${c.error}`).join(' / ') }
  }
  if (h.cameras.length === 0) {
    return {
      id: 'pipeline',
      label: 'Pipeline',
      state: 'warn',
      value: 'Not started in this process',
      detail: 'api.pipeline is disabled or names no camera, so nothing is being analysed right now.',
    }
  }
  if (running.length === 0) {
    return { id: 'pipeline', label: 'Pipeline', state: 'warn', value: 'Configured, none running', detail: `${h.cameras.length} camera(s) listed and none is producing frames.` }
  }
  return {
    id: 'pipeline',
    label: 'Pipeline',
    state: 'ok',
    value: `${running.length}/${h.cameras.length} producing frames`,
    detail: `Frames read into the hub: ${h.frames_in}. Events published: ${h.events_out}.`,
  }
}

export function eventsRow(h: HealthCheck | null): CheckRow {
  if (!h) return { id: 'events', label: 'Event delivery', state: 'unknown', value: 'No answer', detail: 'Health has not been read.' }
  const dropped = h.dropped_events
  if (dropped > 0) {
    return {
      id: 'events',
      label: 'Event delivery',
      state: 'warn',
      value: `${dropped} dropped`,
      detail: 'The bus or a slow reader lost these, so a screen may have missed a bay change.',
    }
  }
  return { id: 'events', label: 'Event delivery', state: 'ok', value: 'Nothing dropped', detail: `${h.events_out} events published, ${h.ws_ticket_refusals} socket ticket(s) refused.` }
}

/** The health page's backend half, in reading order. */
export function backendRows(subject: BackendSubject): CheckRow[] {
  const h = subject.health
  return [apiRow(subject), databaseRow(h), pipelineRow(h), eventsRow(h), gpuRow(h), anprRow(h)]
}

/** What this tab holds, which says nothing about the server's own health. */
export function clientRows(args: { socket: SocketStatus; apiOk: boolean | null; close?: { code: number; reason: string } }): CheckRow[] {
  const why = args.close ? `Closed: ${args.close.reason} (code ${args.close.code}). ` : ''
  const socket: CheckRow =
    args.socket === 'live'
      ? { id: 'socket', label: 'This browser: event channel', state: 'ok', value: 'Open', detail: 'Bay updates arrive on /ws/live.' }
      : args.socket === 'connecting'
        ? { id: 'socket', label: 'This browser: event channel', state: 'warn', value: 'Opening', detail: 'A one-time ticket has been minted and the handshake is in flight.' }
        : args.socket === 'retrying'
          ? { id: 'socket', label: 'This browser: event channel', state: 'warn', value: 'Reconnecting', detail: `${why}A new ticket is minted for the next attempt.` }
          : args.socket === 'offline'
            ? { id: 'socket', label: 'This browser: event channel', state: 'bad', value: 'Closed', detail: args.close ? `${why}Retrying would be refused for the same reason.` : 'No ticket could be minted for it.' }
            : { id: 'socket', label: 'This browser: event channel', state: 'unknown', value: 'Not opened', detail: 'Nothing has asked for a ticket yet.' }

  const rest: CheckRow =
    args.apiOk === null
      ? { id: 'rest', label: 'This browser: REST reads', state: 'unknown', value: 'Reading', detail: 'The first request is still open.' }
      : args.apiOk
        ? { id: 'rest', label: 'This browser: REST reads', state: 'ok', value: 'Answering', detail: 'This page is getting data from the API over HTTP.' }
        : { id: 'rest', label: 'This browser: REST reads', state: 'bad', value: 'Refused', detail: 'The API answered this browser with an error; the server itself may still be healthy.' }

  return [rest, socket]
}

/** One camera's whole story: the row that is configuration and the live state that
 * is a running process. They disagree routinely, so the page shows both. */
export function cameraRows(camera: CameraInfo): CheckRow[] {
  const live: CameraLive = camera.live
  const started = live.streaming || live.frames_available
  return [
    {
      id: `${camera.camera_id}:source`,
      label: `${camera.name || camera.camera_id}: source`,
      state: live.error ? 'bad' : camera.enabled ? 'ok' : 'warn',
      value: live.error ? 'Reader error' : camera.enabled ? 'Enabled' : 'Disabled',
      detail: live.error ?? (camera.source ? `Configured as ${camera.source}` : 'No source path or index is configured.'),
    },
    {
      id: `${camera.camera_id}:stream`,
      label: `${camera.name || camera.camera_id}: frames`,
      state: !started ? 'warn' : live.streaming ? 'ok' : 'warn',
      value: !started
        ? 'Not running in this process'
        : `${live.streaming ? 'Streaming' : 'Idle'} · ${live.frames_processed ?? 0} frame(s) over ${live.passes} pass(es)`,
      detail: [
        live.synthetic_replay ? 'Recorded/replayed source, not a live field camera.' : 'Live camera input.',
        live.read_plates ? 'Plate reading on.' : 'Plate reading off for this stream.',
        live.loop ? 'Looping.' : 'Single pass.',
      ].join(' '),
    },
  ]
}
