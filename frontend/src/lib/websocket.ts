import { requestWsTicket, type WsTicket } from './api'
import type { WSMessage } from './types'

/** The socket's own URL, built where the page was served from.
 *
 * Same-origin by default: the socket connects to the host that served the page,
 * which nginx (and the Vite dev proxy) upgrades and forwards to the API.
 * VITE_WS_BASE overrides it for a bundle served without those proxies.
 *
 * A function and not a module constant because the tests in this project run in
 * node, without a `window`: the value is only needed when a socket opens.
 */
export function socketBase(): string {
  const fromEnv = (import.meta.env.VITE_WS_BASE ?? '').trim()
  if (fromEnv) return fromEnv
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}`
}

/** The multiplexed event channel: bay statuses, sessions, violations, summaries.
 *
 * `/ws/cameras/{id}/overlay` is a different job - it streams per-frame box
 * geometry to draw over the MJPEG picture and carries no bay status at all, so
 * a dashboard that subscribes to it for slot updates never receives one.
 */
export const EVENTS_PATH = '/ws/live'

/** A socket URL with the credential in it, which is the whole reason tickets exist.
 *
 * A browser's `WebSocket` cannot set an `Authorization` header, so this URL is the
 * only place the handshake can carry who is asking - and the query string is what
 * nginx logs, what the browser keeps in history, and what a crash report quotes.
 * So it carries a `POST /auth/ws-ticket` grant: opaque, spent by the first
 * connection that presents it, and expired in `api.ws_ticket_ttl_s` seconds.
 * Never the access token, which was a live credential for fifteen minutes in every
 * one of those copies.
 */
export function eventsUrl(ticket: string, base: string = socketBase()): string {
  return `${base}${EVENTS_PATH}?ticket=${encodeURIComponent(ticket)}`
}

export const CLOSE_UNAUTHORISED = 4401
export const CLOSE_ROLE = 4403

/** The closes a new ticket cannot answer: the account or its role moved, and only
 * a sign-in fixes that. Retrying these is how a signed-out dashboard keeps
 * hammering a server that has already said no. */
export const FATAL_CLOSE_REASONS: readonly string[] = [
  'token_revoked',
  'user_inactive',
  'user_unknown',
  'role_changed',
]

/** Whether a closed socket is worth opening again.
 *
 * A spent or expired ticket (`token_invalid`, `token_missing`, `ticket_required`)
 * is a credential problem the *next* mint fixes, so that reconnects. A normal
 * close (1000/1001 - the server shutting down, the tab moving) and a network drop
 * (code 1006, no reason at all) reconnect too.
 */
export function reconnectAfterClose(code: number, reason: string): boolean {
  if (code === CLOSE_ROLE) return false
  return !FATAL_CLOSE_REASONS.includes(reason)
}

export type SocketStatus = 'idle' | 'connecting' | 'live' | 'retrying' | 'offline'

export interface LiveSocketOptions {
  /** Mints the credential, once per connection attempt. */
  ticket?: () => Promise<WsTicket>
  /** Lets the dashboard say "Live" about the channel rather than about the page. */
  onStatus?: (status: SocketStatus, close?: { code: number; reason: string }) => void
  reconnectDelayMs?: number
}

export class LiveSocket {
  private ws: WebSocket | null = null
  private listeners = new Set<(msg: WSMessage) => void>()
  private timer: number | null = null
  private stopped = true
  private mint: () => Promise<WsTicket>
  private report: (status: SocketStatus, close?: { code: number; reason: string }) => void
  private delay: number

  constructor(opts: LiveSocketOptions = {}) {
    this.mint = opts.ticket ?? requestWsTicket
    this.report = opts.onStatus ?? (() => {})
    this.delay = opts.reconnectDelayMs ?? 10_000
  }

  connect() {
    this.stopped = false
    void this.open()
  }

  disconnect() {
    this.stopped = true
    if (this.timer !== null) {
      clearTimeout(this.timer)
      this.timer = null
    }
    const ws = this.ws
    this.ws = null
    if (ws) {
      // The server's own close (a sign-out, a role change) must not be mistaken
      // for a drop: `disconnect` is the caller taking the channel down.
      ws.onclose = null
      ws.onmessage = null
      ws.close()
    }
  }

  subscribe(cb: (msg: WSMessage) => void): () => void {
    this.listeners.add(cb)
    return () => this.listeners.delete(cb)
  }

  private async open() {
    if (this.stopped) return
    this.report('connecting')

    // One mint per attempt, because a ticket is spent by the connection that uses
    // it: reusing this URL for a reconnect would be refused as already used.
    let ticket: string
    try {
      ticket = (await this.mint()).ticket
    } catch {
      // No credential to offer. The REST queries keep the numbers coming, but this
      // is not "live", and the chip says so.
      this.report('offline')
      return
    }
    if (this.stopped) return

    try {
      const ws = new WebSocket(eventsUrl(ticket))
      this.ws = ws
      ws.onopen = () => this.report('live')
      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data as string) as WSMessage
          this.listeners.forEach((cb) => cb(msg))
        } catch {
          // malformed message - ignore
        }
      }
      ws.onclose = (evt) => {
        if (this.ws !== ws) return // superseded by a newer attempt, or taken down
        this.ws = null
        const close = { code: evt.code, reason: evt.reason }
        if (reconnectAfterClose(close.code, close.reason)) {
          this.report('retrying', close)
          this.schedule()
        } else {
          // A refused session is not a dropped one: the dashboard stops showing
          // "Live" and the API layer's session handling takes it to the sign-in.
          this.report('offline', close)
        }
      }
      ws.onerror = () => {
        // `onclose` always follows an error, and that is where the decision lives.
      }
    } catch {
      this.report('offline')
    }
  }

  private schedule() {
    if (this.stopped || this.timer !== null) return
    this.timer = window.setTimeout(() => {
      this.timer = null
      void this.open()
    }, this.delay)
  }
}
