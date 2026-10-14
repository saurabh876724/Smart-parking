/** Whether the numbers on screen are current, as far as this browser can tell.
 *
 * The problem this exists for: the pipeline runs its first pass over the video
 * file and then goes quiet by configuration (`occupancy.first_pass_events_only`),
 * so a silent socket is normal - and a silent socket next to a dead one look the
 * same unless somebody asks which it is. A dashboard that says "Live" over a
 * two-hour-old status is the lie this module stops.
 */

import type { SocketStatus } from './websocket'

/** Past this, "nothing has changed" is worth saying out loud. */
export const QUIET_AFTER_MS = 20_000

/** Past this, with no event channel to vouch for the data, the numbers are stale
 * rather than merely unchanged, and the screen has to date them. */
export const STALE_AFTER_MS = 120_000

export type FreshnessState =
  /** No bay status has reached this browser yet, from REST or from the socket. */
  | 'waiting'
  /** The channel is open and an event arrived inside the quiet window. */
  | 'live'
  /** The channel is open and nothing has arrived - which, for a finished pass,
   * means no bay changed. */
  | 'quiet'
  /** The channel is closed, so the screen is showing the last REST read. */
  | 'held'
  /** Nothing has updated these numbers for longer than the stale window. */
  | 'stale'

export interface FreshnessInput {
  /** When the last bay status arrived, from either source. Epoch ms. */
  updatedAt: number | null
  /** When the REST bay list was read, if later than the last event. */
  syncedAt?: number | null
  socket: SocketStatus
  now: number
}

export interface Freshness {
  state: FreshnessState
  /** Age of the newest status this browser holds. Null when there is none. */
  ageMs: number | null
  /** What to put next to the numbers, phrased for an operator rather than a log. */
  message: string
  /** True when a status on screen could be wrong without the screen saying so. */
  untrustworthy: boolean
}

export function freshnessOf(input: FreshnessInput): Freshness {
  const at = Math.max(input.updatedAt ?? 0, input.syncedAt ?? 0)
  if (at === 0) {
    return { state: 'waiting', ageMs: null, message: 'Waiting for the first status from the backend.', untrustworthy: true }
  }
  const ageMs = Math.max(0, input.now - at)
  const connected = input.socket === 'live'

  if (ageMs > STALE_AFTER_MS && !connected) {
    return {
      state: 'stale',
      ageMs,
      message: 'Not receiving updates: these statuses are last read as noted and may be behind the lot.',
      untrustworthy: true,
    }
  }
  if (connected && ageMs <= QUIET_AFTER_MS) {
    return { state: 'live', ageMs, message: 'Receiving bay updates.', untrustworthy: false }
  }
  if (connected) {
    return {
      state: 'quiet',
      ageMs,
      message: 'Channel open; no bay has changed since the last update.',
      untrustworthy: false,
    }
  }
  return {
    state: 'held',
    ageMs,
    message: 'Event channel closed - showing the last read until it reconnects.',
    untrustworthy: true,
  }
}

/** A short "HH:MM:SS" stamp for a freshness line, or the words for having none. */
export function asOf(at: number | null): string {
  if (at === null) return 'no data yet'
  return new Date(at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
