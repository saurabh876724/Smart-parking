export type SlotStatus = 'FREE' | 'OCCUPIED' | 'UNKNOWN'

export type SessionStatus = 'ACTIVE' | 'COMPLETED' | 'NEEDS_REVIEW' | 'ANOMALY'

// The envelope the API actually puts on the socket - see
// `src/smart_parking/api/live.py::_payload_for`. `type` is the event bus's own
// vocabulary and `channel` is the coarse group a viewer filters on, so a
// dashboard that reads `msg.slot_id` off the top level is reading a field the
// server has never sent: it lives in `payload`.
export type WSChannel = 'slot' | 'summary' | 'session' | 'violation' | 'plate' | 'camera' | 'other'

export interface WSMessage<P = Record<string, unknown>> {
  type: string
  channel?: WSChannel
  camera_id?: string
  ts?: number
  payload?: P
}

// The three events that carry a bay's committed status.
export const SLOT_EVENT_TYPES: readonly string[] = ['slot_occupied', 'slot_freed', 'slot_snapshot']

// `slot_id` is the bay's code ("A1"), which is how the calibration file, the
// engine and `SlotSnapshot.slot_code` all name it - not its numeric database id.
export interface SlotEventPayload {
  slot_id: string
  status: SlotStatus
  track_id: number | null
  score: number
}

export interface SummaryEventPayload {
  total: number
  occupied: number
  available: number
  unknown: number
}
