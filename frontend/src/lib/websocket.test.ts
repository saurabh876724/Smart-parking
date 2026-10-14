import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CLOSE_ROLE,
  CLOSE_UNAUTHORISED,
  EVENTS_PATH,
  LiveSocket,
  eventsUrl,
  reconnectAfterClose,
  type SocketStatus,
} from './websocket'

/** Enough of a browser socket to see which URL was opened and to close it on cue.
 *
 * The behaviour under test is which credential goes into the URL and what the
 * client does when the server refuses it - neither of which needs a real network.
 */
class FakeSocket {
  static opened: FakeSocket[] = []
  url: string
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number; reason: string }) => void) | null = null
  onerror: (() => void) | null = null
  clientClosed = false

  constructor(url: string) {
    this.url = url
    FakeSocket.opened.push(this)
  }

  close() {
    this.clientClosed = true
  }

  open() {
    this.onopen?.()
  }

  receive(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) })
  }

  shut(code: number, reason = '') {
    this.onclose?.({ code, reason })
  }
}

const ACCESS_TOKEN = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWI6IjEifQ.not-a-live-credential-here'

let statuses: { status: SocketStatus; close?: { code: number; reason: string } }[]
let minted: string[]

function mintTicket(): () => Promise<{ ticket: string; expires_at: string }> {
  return async () => {
    const ticket = `T${minted.length + 1}`
    minted.push(ticket)
    return { ticket, expires_at: '2026-10-12T09:00:15+00:00' }
  }
}

beforeEach(() => {
  FakeSocket.opened = []
  statuses = []
  minted = []
  // `socketBase()` reads the page's own origin, which is what makes the bundle
  // work behind nginx on any host; in node that origin has to be said out loud.
  // The two timers are the process's own - a socket client in node still needs a
  // way to wait, and `vi.useFakeTimers` would only hide the delay it is testing.
  vi.stubGlobal('window', {
    location: { protocol: 'http:', host: 'dashboard.test' },
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
  })
  vi.stubGlobal('WebSocket', FakeSocket)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function connect(overrides: { reconnectDelayMs?: number } = {}) {
  const socket = new LiveSocket({
    ticket: mintTicket(),
    onStatus: (status, close) => statuses.push(close ? { status, close } : { status }),
    reconnectDelayMs: 1,
    ...overrides,
  })
  socket.connect()
  return socket
}

/** Let the minted-ticket promise settle and a scheduled reconnect fire. */
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 20))
}

describe('the socket URL', () => {
  it('carries a ticket and never a bearer token', () => {
    const url = eventsUrl('Tk1', 'ws://dashboard.test')
    expect(url).toBe(`ws://dashboard.test${EVENTS_PATH}?ticket=Tk1`)
    expect(url).not.toContain('token=')
  })

  it('escapes what it puts in the query string', () => {
    // A ticket is base64url, so this is belt-and-braces: the URL is the one place
    // a credential is written into a log file, and a `&` there would be a second
    // parameter somebody else's log line.
    expect(eventsUrl('a+b/c=d', 'ws://h')).toBe('ws://h/ws/live?ticket=a%2Bb%2Fc%3Dd')
  })
})

describe('opening the event channel', () => {
  it('mints a ticket and opens with it, once', async () => {
    connect()
    await settle()

    expect(minted).toEqual(['T1'])
    expect(FakeSocket.opened.map((s) => s.url)).toEqual(['ws://dashboard.test/ws/live?ticket=T1'])
    expect(statuses.map((s) => s.status)).toEqual(['connecting'])
  })

  it('never puts the access token in the URL', async () => {
    // The token is what a bearer credential is made of, and a WebSocket handshake
    // cannot carry a header - so the old client sent the token in the query string
    // and every nginx access log, browser history entry and crash report that
    // quotes the URL got a live fifteen-minute credential with it.
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => (key === 'sp_access_token' ? ACCESS_TOKEN : null),
      setItem: () => {},
      removeItem: () => {},
    })
    connect()
    await settle()

    expect(FakeSocket.opened).toHaveLength(1)
    expect(FakeSocket.opened[0].url).not.toContain(ACCESS_TOKEN)
    expect(FakeSocket.opened[0].url).toContain('ticket=T1')
  })

  it('reports the channel as live only when the server says so', async () => {
    connect()
    await settle()

    expect(statuses.map((s) => s.status)).toEqual(['connecting'])
    FakeSocket.opened[0].open()
    expect(statuses.map((s) => s.status)).toEqual(['connecting', 'live'])
  })

  it('hands a slot message to its subscriber and ignores a broken frame', async () => {
    const socket = connect()
    await settle()
    const seen: unknown[] = []
    socket.subscribe((msg) => seen.push(msg))

    FakeSocket.opened[0].receive({ type: 'slot_occupied', channel: 'slot', payload: { slot_id: 'A1', status: 'OCCUPIED' } })
    expect(seen).toHaveLength(1)

    // A frame that is not JSON is not a reason to stop listening for the next one.
    FakeSocket.opened[0].onmessage?.({ data: 'not json at all' })
    expect(seen).toHaveLength(1)
  })

  it('says it is offline when there is no ticket to offer', async () => {
    const socket = new LiveSocket({
      ticket: async () => {
        throw new Error('token_revoked')
      },
      onStatus: (status, close) => statuses.push(close ? { status, close } : { status }),
    })
    socket.connect()
    await settle()

    expect(FakeSocket.opened).toHaveLength(0)
    expect(statuses.map((s) => s.status)).toEqual(['connecting', 'offline'])
  })
})

describe('reconnecting', () => {
  it('mints a fresh ticket for every attempt', async () => {
    connect()
    await settle()
    expect(minted).toEqual(['T1'])

    // A drop: the server is gone, not the session. 1006 carries no reason.
    FakeSocket.opened[0].shut(1006)
    await settle()

    // A ticket is spent by the connection that presents it, so reconnecting with
    // the same URL would be refused as already used.
    expect(minted).toEqual(['T1', 'T2'])
    expect(FakeSocket.opened.map((s) => s.url)).toEqual([
      'ws://dashboard.test/ws/live?ticket=T1',
      'ws://dashboard.test/ws/live?ticket=T2',
    ])
    expect(statuses.map((s) => s.status)).toEqual(['connecting', 'retrying', 'connecting'])
  })

  it('stops for a credential the server withdrew, and says why', async () => {
    connect()
    await settle()

    FakeSocket.opened[0].shut(CLOSE_UNAUTHORISED, 'token_revoked')
    await settle()

    // Retrying a revocation is a reconnect loop against a sign-out: nothing a new
    // ticket can do, because the account's cutoff is what refused the last one.
    expect(minted).toEqual(['T1'])
    expect(FakeSocket.opened).toHaveLength(1)
    expect(statuses[statuses.length - 1]).toEqual({
      status: 'offline',
      close: { code: CLOSE_UNAUTHORISED, reason: 'token_revoked' },
    })
  })

  it('stops for a role that moved out from under the session', async () => {
    connect()
    await settle()

    FakeSocket.opened[0].shut(CLOSE_ROLE, 'role_changed')
    await settle()

    expect(minted).toEqual(['T1'])
    expect(statuses[statuses.length - 1].status).toBe('offline')
  })

  it('keeps trying for a ticket that simply went stale', async () => {
    connect()
    await settle()

    FakeSocket.opened[0].shut(CLOSE_UNAUTHORISED, 'token_invalid')
    await settle()

    // `token_invalid` is "unknown, already used, or too old" - a new mint is the
    // answer, which is exactly what a slow reconnect needs after fifteen seconds.
    expect(minted).toEqual(['T1', 'T2'])
  })

  it('does not reconnect after the caller takes it down', async () => {
    const socket = connect()
    await settle()

    socket.disconnect()
    await settle()

    expect(FakeSocket.opened[0].clientClosed).toBe(true)
    expect(minted).toEqual(['T1'])
    expect(statuses.map((s) => s.status)).toEqual(['connecting'])
  })
})

describe('the close table', () => {
  it('treats a plain server close as a drop worth recovering from', () => {
    expect(reconnectAfterClose(1000, '')).toBe(true)
    expect(reconnectAfterClose(1001, 'going away')).toBe(true)
    expect(reconnectAfterClose(1006, '')).toBe(true)
    expect(reconnectAfterClose(1011, 'internal error')).toBe(true)
  })

  it('treats a 4403 as fatal whatever it is called', () => {
    // The role close has one meaning: this token's permissions are not this
    // account's permissions any more, and a sign-in is the only repair.
    expect(reconnectAfterClose(CLOSE_ROLE, 'role_changed')).toBe(false)
    expect(reconnectAfterClose(CLOSE_ROLE, '')).toBe(false)
  })

  it('names every reason the server closes for', () => {
    // `live_ws.py` answers with these strings, so a rename there has to be a
    // change here as well - otherwise the client loops on a refusal forever.
    for (const reason of ['token_revoked', 'user_inactive', 'user_unknown', 'role_changed']) {
      expect(reconnectAfterClose(CLOSE_UNAUTHORISED, reason)).toBe(false)
    }
    for (const reason of ['token_missing', 'token_invalid', 'ticket_required']) {
      expect(reconnectAfterClose(CLOSE_UNAUTHORISED, reason)).toBe(true)
    }
  })
})
