import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiRequestError,
  SESSION_END_MESSAGE,
  clearTokens,
  durationMinutes,
  getAccessToken,
  getRefreshToken,
  listCameras,
  listSessions,
  listSlots,
  login,
  logout,
  onSessionEnd,
  plateFor,
  requestWsTicket,
  setTokens,
  streamUrl,
  type SessionEndReason,
  type SessionRecord,
  type TokenPair,
} from './api'

/** A `localStorage` a node process can run without one.
 *
 * The tokens live in the browser's storage because that is the only place a page
 * reload can find them again; these tests run in node, so the three methods this
 * module uses are what the stub has to provide.
 */
class MemoryStorage {
  private items = new Map<string, string>()
  getItem(key: string) {
    return this.items.has(key) ? (this.items.get(key) as string) : null
  }
  setItem(key: string, value: string) {
    this.items.set(key, value)
  }
  removeItem(key: string) {
    this.items.delete(key)
  }
  keys() {
    return [...this.items.keys()]
  }
}

interface Call {
  url: string
  method: string
  auth: string | null
  body: unknown
}

let storage: MemoryStorage
let calls: Call[]

function pair(access: string, refresh: string): TokenPair {
  return { access_token: access, refresh_token: refresh, token_type: 'bearer' }
}

function answer(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A server that answers what a test says and remembers what it was asked.
 *
 * The handler receives how many times *its own path* has been called, which is how
 * "the first try is refused and the retry works" is written down rather than
 * narrated.
 */
function server(handlers: Record<string, (seen: number) => Response>) {
  const seen = new Map<string, number>()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = String(input)
      const path = url.split('?')[0]
      const headers = new Headers(init?.headers)
      calls.push({
        url,
        method: init?.method ?? 'GET',
        auth: headers.get('Authorization'),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      })
      const count = seen.get(path) ?? 0
      seen.set(path, count + 1)
      const handler = handlers[path]
      if (!handler) return answer(404, { error: { code: 'not_found', message: path } })
      return handler(count)
    }),
  )
}

/** 401 with the code the endpoint would use, in the envelope the API really sends. */
function refuse(code: string, message = 'refused') {
  return answer(401, { error: { code, message } })
}

beforeEach(() => {
  calls = []
  storage = new MemoryStorage()
  vi.stubGlobal('localStorage', storage)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the tokens a session is made of', () => {
  it('keeps both halves of a login, because one is how the session goes on', async () => {
    server({ '/api/v1/auth/login': () => answer(200, pair('A1', 'R1')) })

    const result = await login({ email: 'desk@lot.example', password: 'Barrier-Ticket-2026' })

    expect(result.access_token).toBe('A1')
    expect(getAccessToken()).toBe('A1')
    // The refresh token is the half this client used to throw away, which is why a
    // dashboard left open across a break had no way back except a reload.
    expect(getRefreshToken()).toBe('R1')
    expect(storage.keys().sort()).toEqual(['sp_access_token', 'sp_refresh_token'])
  })

  it('signs a call with the access token, never the refresh token', async () => {
    setTokens(pair('A1', 'R1'))
    server({ '/api/v1/cameras': () => answer(200, []) })

    await listCameras()

    expect(calls[0].auth).toBe('Bearer A1')
  })

  it('forgets both when the tokens are cleared', () => {
    setTokens(pair('A1', 'R1'))
    clearTokens()
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()
  })
})

describe('an expired access token', () => {
  it('is refreshed and the original call retried once, without asking the user', async () => {
    setTokens(pair('OLD', 'R1'))
    server({
      '/api/v1/slots': (seen) => (seen === 0 ? refuse('token_expired') : answer(200, [{ id: 1 }])),
      '/api/v1/auth/refresh': () => answer(200, pair('NEW', 'R2')),
    })

    await expect(listSlots('lot_cam1')).resolves.toEqual([{ id: 1 }])

    expect(calls.map((c) => c.url)).toEqual([
      '/api/v1/slots?camera_id=lot_cam1',
      '/api/v1/auth/refresh',
      '/api/v1/slots?camera_id=lot_cam1',
    ])
    expect(calls[1].body).toEqual({ refresh_token: 'R1' })
    expect(calls[2].auth).toBe('Bearer NEW')
    // The pair rotates on every refresh, so the spent `R1` has to be replaced:
    // storing it again would leave the client holding a credential the server has
    // already withdrawn.
    expect(getRefreshToken()).toBe('R2')
  })

  it('is refreshed once even when every query on the page expires together', async () => {
    setTokens(pair('OLD', 'R1'))
    server({
      '/api/v1/slots': (seen) => (seen === 0 ? refuse('token_expired') : answer(200, [])),
      '/api/v1/sessions': (seen) =>
        seen === 0 ? refuse('token_expired') : answer(200, { items: [], total: 0, limit: 10, offset: 0 }),
      '/api/v1/auth/refresh': () => answer(200, pair('NEW', 'R2')),
    })

    await Promise.all([listSlots('lot_cam1'), listSessions({ limit: 10 })])

    // Two refreshes of one rotation is a replay: the second call would spend a
    // token the server had already replaced, and the session would end on a
    // dashboard that was only trying to stay open.
    expect(calls.filter((c) => c.url === '/api/v1/auth/refresh')).toHaveLength(1)
  })

  it('a refusal a refresh cannot mend ends the session instead of retrying', async () => {
    setTokens(pair('A1', 'R1'))
    const ended: SessionEndReason[] = []
    const unsubscribe = onSessionEnd((reason) => ended.push(reason))
    server({
      '/api/v1/slots': () => refuse('token_revoked', 'this credential was signed out'),
      '/api/v1/auth/refresh': () => answer(200, pair('NEW', 'R2')),
    })

    await expect(listSlots('lot_cam1')).rejects.toBeInstanceOf(ApiRequestError)

    // `token_revoked` is the server saying somebody pressed Logout. Refreshing
    // would be refused for the same reason, and a client that keeps retrying is a
    // signed-out dashboard still showing the numbers it last had.
    expect(calls.filter((c) => c.url === '/api/v1/auth/refresh')).toHaveLength(0)
    expect(calls.filter((c) => c.url === '/api/v1/slots?camera_id=lot_cam1')).toHaveLength(1)
    expect(ended).toEqual(['token_revoked'])
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()
    expect(SESSION_END_MESSAGE.token_revoked).toMatch(/signed out/i)
    unsubscribe()
  })

  it('a refresh token that has itself expired ends the session', async () => {
    setTokens(pair('A1', 'R1'))
    const ended: SessionEndReason[] = []
    const unsubscribe = onSessionEnd((reason) => ended.push(reason))
    server({
      '/api/v1/slots': (seen) => (seen === 0 ? refuse('token_expired') : answer(200, [])),
      '/api/v1/auth/refresh': () => refuse('token_expired', 'refresh token has expired'),
    })

    await expect(listSlots('lot_cam1')).rejects.toMatchObject({ code: 'token_expired' })

    expect(ended).toEqual(['token_expired'])
    expect(getAccessToken()).toBeNull()
    unsubscribe()
  })

  it('a server it cannot reach does not log the operator out', async () => {
    setTokens(pair('A1', 'R1'))
    const ended: SessionEndReason[] = []
    const unsubscribe = onSessionEnd((reason) => ended.push(reason))
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('fetch failed')
      }),
    )

    await expect(listSessions({ limit: 10 })).rejects.toBeInstanceOf(TypeError)

    // No answer is not a refusal. The tokens stay and the session is not ended, so
    // the dashboard shows its own error instead of sending a watching operator
    // through a sign-in because a container restarted.
    expect(ended).toEqual([])
    expect(getAccessToken()).toBe('A1')
    expect(getRefreshToken()).toBe('R1')
    unsubscribe()
  })

  it('a role change ends the session, because only a sign-in carries the new role', async () => {
    setTokens(pair('A1', 'R1'))
    const ended: SessionEndReason[] = []
    const unsubscribe = onSessionEnd((reason) => ended.push(reason))
    server({
      '/api/v1/slots': () => answer(403, { error: { code: 'role_changed', message: 'role moved' } }),
    })

    await expect(listSlots('lot_cam1')).rejects.toMatchObject({ status: 403, code: 'role_changed' })

    expect(ended).toEqual(['role_changed'])
    expect(getAccessToken()).toBeNull()
    unsubscribe()
  })
})

describe('logout', () => {
  it('asks the server to withdraw the credentials, then forgets them here', async () => {
    setTokens(pair('A1', 'R1'))
    const ended: SessionEndReason[] = []
    const unsubscribe = onSessionEnd((reason) => ended.push(reason))
    server({
      '/api/v1/auth/logout': () =>
        answer(200, { revoked_at: '2026-10-12T09:00:01+00:00', email: 'desk@lot.example' }),
    })

    await logout()

    expect(calls[0]).toMatchObject({ url: '/api/v1/auth/logout', method: 'POST', auth: 'Bearer A1' })
    expect(ended).toEqual(['logout'])
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()
    unsubscribe()
  })

  it('still signs the browser out when the server refuses', async () => {
    setTokens(pair('A1', 'R1'))
    server({ '/api/v1/auth/logout': () => answer(500, { error: { code: 'boom', message: 'no' } }) })

    await logout()

    // The button has to work: a database that will not write an audit row is not a
    // reason to keep a wall display signed in.
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()
  })
})

describe('the socket ticket', () => {
  it('is asked for over an authenticated POST', async () => {
    setTokens(pair('A1', 'R1'))
    server({
      '/api/v1/auth/ws-ticket': () =>
        answer(200, { ticket: 'Tk1', expires_at: '2026-10-12T09:00:15+00:00' }),
    })

    const ticket = await requestWsTicket()

    expect(calls[0]).toMatchObject({ url: '/api/v1/auth/ws-ticket', method: 'POST', auth: 'Bearer A1' })
    expect(ticket.ticket).toBe('Tk1')
    expect(ticket.expires_at).toContain('+00:00')
  })
})

describe('the camera picture URL', () => {
  it('carries the access token in the query, because an <img> has no headers', () => {
    setTokens(pair('A1', 'R1'))
    expect(streamUrl('lot_cam1')).toBe('/api/v1/cameras/lot_cam1/stream.mjpg?token=A1')
  })

  it('refuses to build a URL it has no credential for', () => {
    // A request without a token is a guaranteed 401, and an image cannot report
    // which status it got: the component has to be told to say "not signed in".
    expect(streamUrl('lot_cam1')).toBeNull()
    setTokens(pair('A1', 'R1'))
    expect(streamUrl('lot_cam1')).toContain('token=A1')
    clearTokens()
    expect(streamUrl('lot_cam1')).toBeNull()
  })

  it('keeps a camera id inside its own path segment', () => {
    setTokens(pair('A1', 'R1'))
    // Camera ids come from configuration rather than a user, but a URL built by
    // concatenation would let one of them change the path or the query.
    expect(streamUrl('a/b?x=1')).toBe('/api/v1/cameras/a%2Fb%3Fx%3D1/stream.mjpg?token=A1')
  })

  it('sends the refresh token nowhere near a URL', () => {
    setTokens(pair('A1', 'R1'))
    expect(streamUrl('lot_cam1')).not.toContain('R1')
  })
})

describe('the sessions page the dashboard reads', () => {
  /** A row exactly as `GET /sessions` answers it (`schemas.py::SessionResponse`).
   *
   * `satisfies` is the assertion: a field this interface no longer matches - added,
   * renamed, dropped - stops the build instead of quietly blanking a column. The
   * server side is pinned by the same list in
   * `tests/unit/test_api_endpoints.py`.
   */
  const ROW = {
    id: 12,
    source_session_id: 'gate-0004',
    camera_id: 'gate_cam1',
    entry_track_id: 15,
    exit_track_id: 16,
    plate: null,
    plate_masked: 'MH20**1234',
    vehicle_type: 'car',
    entry_time: '2026-10-12T08:00:00+00:00',
    exit_time: '2026-10-12T08:50:00+00:00',
    duration_seconds: 3000.0,
    status: 'COMPLETED',
    exit_reason: 'line',
    plate_confidence: 0.87,
    ocr_confidence: 0.91,
    review_reason: null,
    anomaly_reason: null,
  } satisfies SessionRecord

  it('asks for a page with the parameters the endpoint actually takes', async () => {
    setTokens(pair('A1', 'R1'))
    server({
      '/api/v1/sessions': () => answer(200, { items: [ROW], total: 4, limit: 10, offset: 0 }),
    })

    const page = await listSessions({ limit: 10 })
    expect(calls[0].url).toBe('/api/v1/sessions?limit=10')
    expect(page.total).toBe(4)
    expect(page.items[0].duration_seconds).toBe(3000)

    await listSessions({ status: 'ACTIVE', limit: 1 })
    expect(calls[1].url).toBe('/api/v1/sessions?limit=1&status=ACTIVE')

    // `page` and `page_size` are not parameters of this endpoint: FastAPI ignores
    // what it does not know, so the client that sent them was asking for the
    // default page and reading `total` as if it were the page size.
    expect(calls.map((c) => c.url).join()).not.toContain('page')
  })

  it('answers the count of a filter rather than the count of a page', async () => {
    setTokens(pair('A1', 'R1'))
    server({
      '/api/v1/sessions': () => answer(200, { items: [], total: 17, limit: 1, offset: 0 }),
    })

    // The "Active Sessions" card reads `total` from a `status=ACTIVE&limit=1` call,
    // which is the number of visits the server says are active - not one, and not
    // the ten rows the table happens to show.
    const page = await listSessions({ status: 'ACTIVE', limit: 1 })
    expect(page.items).toHaveLength(0)
    expect(page.total).toBe(17)
  })

  it('shows the plate a role may see and the mask the others get', () => {
    // An OPERATOR/ADMIN row carries both fields; a VIEWER or CUSTOMER row is
    // `plate: null` by design, so a table that read only `plate` was blank for
    // every account that is not staff - and blank looks like an empty lot.
    expect(plateFor(ROW)).toBe('MH20**1234')
    expect(plateFor({ ...ROW, plate: 'MH20AB1234' })).toBe('MH20AB1234')
    expect(plateFor({ ...ROW, plate_masked: null })).toBeNull()
  })

  it('reports a visit that took under a minute as zero, not as unmeasured', () => {
    expect(durationMinutes(null)).toBeNull()
    expect(durationMinutes(0)).toBe(0)
    expect(durationMinutes(20)).toBe(0)
    expect(durationMinutes(80)).toBe(1)
    expect(durationMinutes(ROW.duration_seconds)).toBe(50)
  })

  it('refuses a 422 with the field name and nothing else', async () => {
    setTokens(pair('A1', 'R1'))
    server({
      '/api/v1/sessions': () =>
        answer(422, { error: { code: 'request_invalid', message: 'limit: Input should be less than or equal to 500' } }),
    })

    const err = await listSessions({ limit: 10 }).catch((e) => e)

    expect(err).toBeInstanceOf(ApiRequestError)
    expect((err as ApiRequestError).code).toBe('request_invalid')
    expect((err as ApiRequestError).status).toBe(422)
  })
})
