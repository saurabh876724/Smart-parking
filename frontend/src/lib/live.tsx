import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { listCameras, listSlots, type CameraInfo, type SlotSnapshot } from './api'
import { applySlotMessage, countSlots, type SlotCounts } from './slots'
import { LiveSocket, type SocketStatus } from './websocket'
import { freshnessOf, type Freshness } from './freshness'
import { SLOT_EVENT_TYPES } from './types'

/** The camera the server starts its own pipeline for, and so the one with a
 * picture, a calibrated bay list and live events. Configuration, not a guess: the
 * list below comes from `GET /cameras`. */
export const FALLBACK_CAMERA = 'lot_cam1'

/** How often the bay list is re-read while the socket is the fast path.
 *
 * The pipeline goes quiet once its pass over the video file ends, so without a
 * periodic read a screen left open would be holding the statuses from whenever the
 * lot last changed - correct as of then, and not labelled as such. */
const SLOT_POLL_MS = 20_000

/** How often the "how old is this picture" line is re-asked. Ten seconds is short
 * next to the two-minute stale window it feeds. */
const FRESHNESS_TICK_MS = 10_000

export interface LiveValue {
  cameras: CameraInfo[]
  cameraId: string
  setCameraId: (id: string) => void
  slots: SlotSnapshot[]
  counts: SlotCounts
  loading: boolean
  /** The bay read failed, as opposed to the bay list being legitimately empty. */
  error: Error | null
  socket: SocketStatus
  socketClose: { code: number; reason: string } | undefined
  /** Epoch ms of the newest status this browser holds, from either source. */
  updatedAt: number | null
  freshness: Freshness
  /** Epoch ms of the latest slot event this tab received, for the live page's
   * "last change" column. */
  lastEventAt: number | null
  /** The bay changes this tab has watched arrive, newest first, capped at twelve.
   * Nothing older than the page load: the event channel replays nothing. */
  recent: SlotChange[]
  refresh: () => void
}

export interface SlotChange {
  at: number
  slotCode: string
  status: string
  trackId: number | null
}

const LiveContext = createContext<LiveValue | null>(null)

export function LiveProvider({ children }: { children: ReactNode }) {
  const [cameraId, setCameraId] = useState(FALLBACK_CAMERA)
  const [slots, setSlots] = useState<SlotSnapshot[]>([])
  const [socket, setSocket] = useState<{ status: SocketStatus; close?: { code: number; reason: string } }>({
    status: 'idle',
  })
  const [stamp, setStamp] = useState<{ updatedAt: number | null; lastEventAt: number | null }>({
    updatedAt: null,
    lastEventAt: null,
  })
  const [recent, setRecent] = useState<SlotChange[]>([])
  const queryClient = useQueryClient()
  // The socket is opened once for the session and lives across camera switches, so
  // it needs the selection and the current bay list in refs rather than in its
  // dependency list - a state updater has to stay pure.
  const cameraRef = useRef(cameraId)
  const slotsRef = useRef<SlotSnapshot[]>([])
  useEffect(() => {
    cameraRef.current = cameraId
    // Drop the previous lot's bays rather than leaving them on screen while the
    // new camera's list loads: two lots' statuses under one heading is exactly the
    // stale data this guard exists to prevent.
    slotsRef.current = []
    setSlots([])
    setRecent([])
    setStamp({ updatedAt: null, lastEventAt: null })
  }, [cameraId])

  const { data: cameras } = useQuery({
    queryKey: ['cameras'],
    queryFn: listCameras,
    staleTime: 60_000,
  })

  const {
    data: slotsData,
    error: slotsError,
    isLoading,
  } = useQuery({
    queryKey: ['slots', cameraId],
    queryFn: () => listSlots(cameraId),
    refetchInterval: SLOT_POLL_MS,
  })

  useEffect(() => {
    if (slotsData) {
      slotsRef.current = slotsData
      setSlots(slotsData)
      setStamp((prev) => ({ ...prev, updatedAt: Date.now() }))
    }
  }, [slotsData])

  useEffect(() => {
    // The event channel, not the per-camera overlay stream: committed bay statuses
    // are published on /ws/live (`api/live.py`).
    const channel = new LiveSocket({
      onStatus: (status, close) => setSocket(close ? { status, close } : { status }),
    })
    const unsubscribe = channel.subscribe((msg) => {
      if (msg.channel !== 'slot' || !SLOT_EVENT_TYPES.includes(msg.type)) return
      if (msg.camera_id && msg.camera_id !== cameraRef.current) return
      const next = applySlotMessage(slotsRef.current, msg)
      if (!next) return
      const at = Date.now()
      const payload = msg.payload as { slot_id?: string; status?: string; track_id?: number | null }
      slotsRef.current = next
      setSlots(next)
      setStamp({ updatedAt: at, lastEventAt: at })
      if (payload?.slot_id && payload.status) {
        setRecent((prev) =>
          [{ at, slotCode: payload.slot_id as string, status: payload.status as string, trackId: payload.track_id ?? null }, ...prev].slice(0, 12),
        )
      }
    })
    channel.connect()
    return () => {
      unsubscribe()
      channel.disconnect()
    }
  }, [])

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['slots', cameraRef.current] })
  }, [queryClient])

  const counts = useMemo(() => countSlots(slots), [slots])

  // A clock that ticks in state rather than `Date.now()` during render: the age of
  // the newest status has to keep moving even while nothing arrives to re-render
  // the page, which is precisely the situation a stale picture creates.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), FRESHNESS_TICK_MS)
    return () => window.clearInterval(id)
  }, [])

  const freshness = useMemo(
    () => freshnessOf({ updatedAt: stamp.updatedAt, socket: socket.status, now }),
    [stamp.updatedAt, socket.status, now],
  )

  const value: LiveValue = {
    cameras: cameras ?? [],
    cameraId,
    setCameraId,
    slots,
    counts,
    loading: isLoading,
    error: slotsError ? new Error(slotsError.message) : null,
    socket: socket.status,
    socketClose: socket.close,
    updatedAt: stamp.updatedAt,
    lastEventAt: stamp.lastEventAt,
    freshness,
    recent,
    refresh,
  }

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}

/** The live state, or a clear failure: a page that renders without the provider
 * would show zeros and call them a quiet lot. */
export function useLive(): LiveValue {
  const value = useContext(LiveContext)
  if (!value) throw new Error('useLive must be used inside LiveProvider')
  return value
}
