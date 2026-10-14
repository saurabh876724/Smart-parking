import type { SlotStatus, SessionStatus } from './types'

// Same-origin by default: nginx (and the Vite dev proxy) forward /api to the
// API, so the bundle works on any host without a rebuild. Set VITE_API_BASE
// only when serving the bundle from somewhere without those proxies.
const API_BASE = (import.meta.env.VITE_API_BASE ?? '').trim() || '/api/v1'

const ACCESS_KEY = 'sp_access_token'
const REFRESH_KEY = 'sp_refresh_token'

interface LoginRequest {
  email: string
  password: string
}

/** What `POST /auth/login` and `POST /auth/refresh` answer with.
 *
 * Both tokens are kept: the access token is fifteen minutes of permission and the
 * refresh token is the one thing that can extend a session without a password.
 * Dropping the second - which is what this file used to do - meant a dashboard
 * left open across a shift break came back to a dead token and no way to recover
 * except a reload.
 */
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  access_expires_at?: string
  role?: string
  email?: string
}

/** A refused request that still carries the HTTP status, so a caller can tell
 * "not implemented yet" (501) from "not allowed" (403) without parsing text.
 *
 * `code` is the server's own error name (`error.code` in the envelope), which is
 * the only part of a 401 a client can branch on: `token_expired` means "get a new
 * one", `token_revoked` means "you signed out somewhere else", and treating those
 * two the same way is what makes a dashboard show stale data for an hour.
 */
export class ApiRequestError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.code = code
  }
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY)
}

export function setTokens(pair: { access_token: string; refresh_token: string }) {
  localStorage.setItem(ACCESS_KEY, pair.access_token)
  localStorage.setItem(REFRESH_KEY, pair.refresh_token)
}

export function clearTokens() {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

export type SessionEndReason =
  | 'logout'
  | 'token_revoked'
  | 'token_expired'
  | 'token_missing'
  | 'token_invalid'
  | 'token_wrong_use'
  | 'user_inactive'
  | 'user_unknown'
  | 'role_changed'

export const SESSION_END_MESSAGE: Record<SessionEndReason, string> = {
  logout: 'Signed out.',
  token_revoked: 'This session was signed out. Sign in again.',
  token_expired: 'Your session expired. Sign in again.',
  token_missing: 'No credentials for this browser. Sign in again.',
  token_invalid: 'This browser holds a credential the server does not recognise.',
  token_wrong_use: 'This browser is holding the wrong kind of token.',
  user_inactive: 'This account has been deactivated.',
  user_unknown: 'This account no longer exists.',
  role_changed: 'Your role changed; sign in again for the new permissions.',
}

type EndListener = (reason: SessionEndReason) => void
const endListeners = new Set<EndListener>()

/** Who to tell when the server says this session is over - see `endSession`. */
export function onSessionEnd(cb: EndListener): () => void {
  endListeners.add(cb)
  return () => endListeners.delete(cb)
}

function endSession(reason: SessionEndReason) {
  clearTokens()
  // A copy: a listener may unsubscribe while the loop runs, and one browser tab
  // ending its session must not stop the others from hearing about it.
  for (const cb of [...endListeners]) cb(reason)
}

/** The 401/403 codes a refresh cannot fix: the account moved, or the credential
 * was withdrawn, and only a sign-in is an answer. */
const SESSION_OVER: Set<string> = new Set([
  'token_revoked',
  'token_missing',
  'token_invalid',
  'token_wrong_use',
  'user_inactive',
  'user_unknown',
  'role_changed',
])

async function errorFrom(res: Response): Promise<ApiRequestError> {
  const body = await res.json().catch(() => ({}))
  return new ApiRequestError(
    res.status,
    body?.error?.code ?? `http_${res.status}`,
    body?.error?.message ?? `HTTP ${res.status}`,
  )
}

/** One call, one header, no retry logic - so the retry below has something plain
 * to retry. */
async function send(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers)
  headers.set('Content-Type', 'application/json')
  const token = getAccessToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  return fetch(`${API_BASE}${path}`, { ...init, headers })
}

let refreshInFlight: Promise<boolean> | null = null

/** Buy a new access token with the refresh token, once per expiry.
 *
 * The pair rotates on every refresh, so two calls that both wake up to an expired
 * access token must not both spend the refresh token they read a moment earlier -
 * the second would be replaying a credential the server has already withdrawn.
 * One shared promise is what makes the second wait for the first.
 */
async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return false
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = (async () => {
    const res = await send('/auth/refresh', {
      method: 'POST',
      body: JSON.stringify({ refresh_token: refreshToken }),
    }).catch(() => null)
    if (res === null) return false // no answer: keep the session, let the caller show an error
    if (!res.ok) {
      const err = await errorFrom(res)
      if (err.code === 'token_expired') endSession('token_expired')
      else if (SESSION_OVER.has(err.code)) endSession(err.code as SessionEndReason)
      return false
    }
    const pair = (await res.json()) as TokenPair
    setTokens(pair)
    return true
  })().finally(() => {
    refreshInFlight = null
  })
  return refreshInFlight
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res = await send(path, init)
  if (!res.ok) {
    const err = await errorFrom(res)
    // Only a lapsed - or never-minted - access token is worth refreshing: the other
    // refusals say the account or the credential is gone, which a refresh repeats
    // and cannot mend.
    const refreshable = err.code === 'token_expired' || err.code === 'token_missing'
    if (res.status === 401 && refreshable && (await refreshAccessToken())) {
      res = await send(path, init)
      if (res.ok) return res.json() as Promise<T>
      const after = await errorFrom(res)
      if (after.status === 401 && after.code === 'token_expired') endSession('token_expired')
      throw after
    }
    // The session is over here, now, rather than in whatever screen notices that
    // the numbers have stopped changing.
    if ((res.status === 401 || res.status === 403) && SESSION_OVER.has(err.code)) {
      endSession(err.code as SessionEndReason)
    }
    throw err
  }
  return res.json() as Promise<T>
}

export async function login({ email, password }: LoginRequest): Promise<TokenPair> {
  const pair = await request<TokenPair>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  setTokens(pair)
  return pair
}

/** Ask the server to withdraw this account's credentials, then forget them here.
 *
 * The local clear is what the user sees; the call is what makes it true. Without
 * it the access token stays valid for its remaining minutes and the refresh token
 * for its remaining twelve hours, so a second tab - or a copy of either - keeps
 * the session alive after the button said "Logout".
 */
export async function logout(): Promise<void> {
  try {
    await request<{ revoked_at: string; email: string }>('/auth/logout', { method: 'POST' })
  } catch {
    // A sign-out the server refused is still a sign-out for this browser: the
    // tokens go either way, and a refusal by name has already ended the session.
  }
  endSession('logout')
}

export interface WsTicket {
  ticket: string
  expires_at: string
}

/** A one-time credential for a socket URL, minted per connection.
 *
 * A browser cannot put an `Authorization` header on a WebSocket handshake, so the
 * credential has to travel in the query string - and the query string is written
 * into nginx's access log, into browser history, and into anything that quotes a
 * failed request. A ticket is opaque, spent by the first connection that presents
 * it, and dead in `api.ws_ticket_ttl_s` seconds (see `api/tickets.py`).
 */
export async function requestWsTicket(): Promise<WsTicket> {
  return request<WsTicket>('/auth/ws-ticket', { method: 'POST' })
}

/** What the running pipeline says about one camera (`CameraLiveResponse`).
 *
 * Kept apart from the configuration row because the two disagree in normal
 * operation: a camera can be registered and disabled, or enabled and not in
 * `api.pipeline.cameras` so nothing ever starts it. `synthetic_replay` is the
 * honest-state flag - when it is true the picture is a rendered fixture pass, not
 * a field camera.
 */
export interface CameraLive {
  camera_id: string
  streaming: boolean
  frames_available: boolean
  role: string
  synthetic_replay: boolean
  read_plates: boolean
  loop: boolean
  passes: number
  frames_processed: number | null
  error: string | null
}

/** One registered camera: its row plus the live state, merged by `GET /cameras`. */
export interface CameraInfo {
  camera_id: string
  name: string
  role: string
  source: string | null
  slots_file: string | null
  gate_file: string | null
  fps_limit: number
  enabled: boolean
  last_seen_at: string | null
  live: CameraLive
}

export async function listCameras(): Promise<CameraInfo[]> {
  return request<CameraInfo[]>('/cameras')
}

/** The annotated MJPEG feed for one camera, ready for an `<img src>`.
 *
 * `?token=` is the only credential a browser will put on an image request - an
 * `img` element cannot set an `Authorization` header - and `deps.viewer_user`
 * accepts it for this endpoint alone. The exposure is bounded twice over: the
 * token is the fifteen-minute access one, and nginx logs these requests without
 * their query strings.
 *
 * Null means this browser holds no access token, so the caller says "not signed
 * in" rather than sending a request the server must refuse with a 401 no image
 * element can report.
 */
export function streamUrl(cameraId: string): string | null {
  const token = getAccessToken()
  if (!token) return null
  return (
    `${API_BASE}/cameras/${encodeURIComponent(cameraId)}/stream.mjpg` +
    `?token=${encodeURIComponent(token)}`
  )
}

/** The JSON half of the newest processed frame (`GET /cameras/{id}/overlay`).
 *
 * The same boxes the MJPEG picture has burned into it, as data - which is what
 * lets a table name the track ids and confidences behind the picture. 409
 * `no_frame` until the pipeline has produced one for that camera.
 */
export interface OverlayTrack {
  track_id: number
  bbox: number[]
  confidence: number
  label: string
}

export interface OverlayFrame {
  camera_id: string
  seq: number
  ts: number
  summary: Record<string, unknown>
  tracks: OverlayTrack[]
}

export async function cameraOverlay(cameraId: string): Promise<OverlayFrame> {
  return request<OverlayFrame>(`/cameras/${encodeURIComponent(cameraId)}/overlay`)
}

export interface SlotSnapshot {
  id: number
  camera_id: string
  slot_code: string
  slot_type: string
  current_status: SlotStatus
  status_since: string | null
  occupied_by_track_id: number | null
  polygon: number[][]
  /** `source_session_id` of the live visit holding this bay, or null.
   * `SlotResponse.session_id` is a string: the identifier the gate pipeline mints,
   * not the numeric row id. */
  session_id: string | null
}

export async function listSlots(cameraId?: string): Promise<SlotSnapshot[]> {
  const query = cameraId ? `?camera_id=${encodeURIComponent(cameraId)}` : ''
  return request<SlotSnapshot[]>(`/slots${query}`)
}

/** Bay counts for one lot as the server commits them (`LotSummaryResponse`).
 *
 * A second source of the same three numbers on purpose: the overview cards count
 * the rows the socket keeps updating, this one reads the database, and a
 * dashboard that shows both can say which of them has drifted.
 */
export interface LotSummary {
  camera_id: string
  total: number
  occupied: number
  available: number
  /** Bays whose *type* is RESERVED - not a fourth status the FSM defines. */
  reserved: number
  unknown: number
  /** FREE over the bays with a committed state, so a camera outage cannot read
   * as spare capacity. */
  occupancy_rate: number
  from_live_frames: boolean
}

export async function lotSummary(cameraId: string): Promise<LotSummary> {
  return request<LotSummary>(`/lots/${encodeURIComponent(cameraId)}/summary`)
}

/** A visit, exactly as `GET /sessions` answers it (`schemas.py::SessionResponse`).
 *
 * The names matter more than the shape here: this row used to be typed as
 * `{session_id, plate_number, duration_s}` and read as `{page, page_size}`, and
 * every one of those five cells rendered empty because `fetch` answers `any` and
 * `tsc` has nothing to check that against. `tests/unit/test_api_endpoints.py` pins
 * the same list of names from the server side.
 */
export interface SessionRecord {
  id: number
  source_session_id: string
  camera_id: string
  entry_track_id: number
  exit_track_id: number | null
  /** The full number, for a role allowed to reveal it; null for the others. */
  plate: string | null
  plate_masked: string | null
  vehicle_type: string | null
  entry_time: string | null
  exit_time: string | null
  duration_seconds: number | null
  status: SessionStatus
  exit_reason: string | null
  plate_confidence: number | null
  ocr_confidence: number | null
  review_reason: string | null
  anomaly_reason: string | null
}

/** What a cell should show for a plate: the number when the role may see it, the
 * mask when it may not, and nothing at all when nothing was read. */
export function plateFor(row: { plate: string | null; plate_masked: string | null }): string | null {
  return row.plate ?? row.plate_masked ?? null
}

/** Minutes parked, or null when the visit has no measured duration. `0` is a real
 * answer - a car that came and went inside a minute - so this cannot be a `?:`. */
export function durationMinutes(seconds: number | null): number | null {
  return seconds === null || Number.isNaN(seconds) ? null : Math.round(seconds / 60)
}

export interface SessionsPage {
  items: SessionRecord[]
  total: number
  limit: number
  offset: number
}

export async function listSessions(params?: {
  limit?: number
  offset?: number
  status?: SessionStatus
  camera_id?: string
  plate?: string
  /** ISO timestamps; the server compares them against its naive UTC rows. */
  from?: string
  to?: string
}): Promise<SessionsPage> {
  const qs = new URLSearchParams()
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.offset) qs.set('offset', String(params.offset))
  if (params?.status) qs.set('status', params.status)
  if (params?.camera_id) qs.set('camera_id', params.camera_id)
  if (params?.plate) qs.set('plate', params.plate)
  if (params?.from) qs.set('from', params.from)
  if (params?.to) qs.set('to', params.to)
  const query = qs.toString()
  return request<SessionsPage>(`/sessions${query ? `?${query}` : ''}`)
}

/** Billing is a later phase, so `GET /stats/revenue` answers 501 `not_implemented`
 * by design. The type exists so a client can *not* pretend it has data: no UI in
 * this app renders a revenue figure, and Analytics says "Not implemented". */
export interface RevenuePoint {
  date: string
  amount: number
}

export async function revenueSummary(days = 7): Promise<{ items: RevenuePoint[] }> {
  return request<{ items: RevenuePoint[] }>(`/stats/revenue?days=${days}`)
}

/** One sample of `GET /stats/occupancy`: the bucket's start and how many bays were
 * held at that instant. `occupied` counts recorded intervals only, so a bay in use
 * right now has no row yet - its truth is the live status, not this series. */
export interface OccupancyPoint {
  at: string
  occupied: number
}

export interface OccupancySeries {
  camera_id: string | null
  bucket: string
  points: OccupancyPoint[]
  /** The endpoint states which of the several meanings of "occupancy over time" it
   * picked; a chart that drops it is a chart that quietly picks another. */
  definition: string
  intervals_considered: number
}

export async function occupancySeries(params?: {
  cameraId?: string
  from?: string
  to?: string
  bucket?: 'hour' | 'day'
}): Promise<OccupancySeries> {
  const qs = new URLSearchParams()
  if (params?.cameraId) qs.set('camera_id', params.cameraId)
  if (params?.from) qs.set('from', params.from)
  if (params?.to) qs.set('to', params.to)
  if (params?.bucket) qs.set('bucket', params.bucket)
  const query = qs.toString()
  return request<OccupancySeries>(`/stats/occupancy${query ? `?${query}` : ''}`)
}

export interface DurationStats {
  /** Null with `sample_size: 0`: an average of nothing is not zero minutes. */
  average_s: number | null
  median_s: number | null
  longest_s: number | null
  sample_size: number
  arrivals_considered: number
  peak_hours: { hour_of_day: number; arrivals: number }[]
}

export async function durationStats(params?: { from?: string; to?: string }): Promise<DurationStats> {
  const qs = new URLSearchParams()
  if (params?.from) qs.set('from', params.from)
  if (params?.to) qs.set('to', params.to)
  const query = qs.toString()
  return request<DurationStats>(`/stats/duration${query ? `?${query}` : ''}`)
}

/** `GET /health` verbatim (`HealthResponse`). The camera list here is only the
 * cameras **this process** started, so `/cameras` is the one to compare against. */
export interface GpuInfo {
  available: boolean
  reason?: string
  torch_version?: string
  cuda_version?: string
  device?: string
  device_count?: number
  memory_total_mib?: number
  memory_free_mib?: number
}

export interface HealthCheck {
  status: 'ok' | 'degraded'
  /** A sentence, not a boolean: "ok", "migrations pending: x", or "unreachable: …". */
  database: string
  anpr_enabled: boolean
  gpu: GpuInfo
  cameras: CameraLive[]
  ws_clients: number
  frames_in: number
  events_out: number
  /** Events the bus dropped before a consumer saw them plus viewers too slow to
   * read them; anything above zero means the dashboard is missing updates. */
  dropped_events: number
  ws_ticket_refusals: number
  uptime_s: number
}

export async function health(): Promise<HealthCheck> {
  return request<HealthCheck>('/health')
}

/** `GET /auth/me`: who this token belongs to. Asked once per session rather than
 * remembered from the login response, so a role change shows up on reload instead
 * of staying wrong until the token expires. */
export interface MeProfile {
  id: number
  email: string
  role: string
  is_active: boolean
  created_at: string | null
  reveal_plates: boolean
}

export async function fetchMe(): Promise<MeProfile> {
  return request<MeProfile>('/auth/me')
}
