import { describe, expect, it } from 'vitest'
import type { SlotSnapshot } from './api'
import { applySlotMessage, classifySlotStatus, countSlots } from './slots'
import type { WSMessage } from './types'

function bay(code: string, status: SlotSnapshot['current_status']): SlotSnapshot {
  return {
    id: code.charCodeAt(0),
    camera_id: 'lot_cam1',
    slot_code: code,
    slot_type: 'REGULAR',
    current_status: status,
    status_since: null,
    occupied_by_track_id: null,
    polygon: [],
    session_id: null,
  }
}

/** Exactly what the dashboard showed when this was reported: five bays, a live
 * camera, three cars parked and two bays nobody had ever reported on. */
const REPORTED = [
  bay('A1', 'UNKNOWN'),
  bay('A2', 'OCCUPIED'),
  bay('B1', 'OCCUPIED'),
  bay('B2', 'OCCUPIED'),
  bay('B3', 'UNKNOWN'),
]

function slotMessage(type: string, code: string, status: string): WSMessage {
  return {
    type,
    channel: 'slot',
    camera_id: 'lot_cam1',
    ts: 1000,
    payload: { slot_id: code, status, track_id: null, score: 0 },
  }
}

describe('classifySlotStatus', () => {
  it('separates the two measured states', () => {
    expect(classifySlotStatus('OCCUPIED')).toBe('occupied')
    expect(classifySlotStatus('FREE')).toBe('available')
  })

  it('never treats UNKNOWN as available', () => {
    expect(classifySlotStatus('UNKNOWN')).toBe('unknown')
  })

  it('buckets a status it does not recognise as unknown, not as free', () => {
    // A debounce state has no business reaching a committed row, and neither
    // does a value added to the enum before this bundle was rebuilt.
    expect(classifySlotStatus('PENDING_FREE')).toBe('unknown')
    expect(classifySlotStatus('')).toBe('unknown')
    expect(classifySlotStatus('RESERVED')).toBe('unknown')
  })
})

describe('countSlots', () => {
  it('counts the reported lot without folding the unreported bays into available', () => {
    expect(countSlots(REPORTED)).toEqual({
      total: 5,
      occupied: 3,
      available: 0,
      unknown: 2,
    })
  })

  it('adds up to the bays it was given', () => {
    const counts = countSlots(REPORTED)
    expect(counts.occupied + counts.available + counts.unknown).toBe(counts.total)
  })

  it('holds for every mix of the three states', () => {
    const states: SlotSnapshot['current_status'][] = ['FREE', 'OCCUPIED', 'UNKNOWN']
    for (const a of states) {
      for (const b of states) {
        const counts = countSlots([bay('A1', a), bay('A2', b)])
        expect(counts.occupied + counts.available + counts.unknown).toBe(2)
      }
    }
  })

  it('reports an empty lot as empty rather than as nothing', () => {
    expect(countSlots([])).toEqual({ total: 0, occupied: 0, available: 0, unknown: 0 })
  })

  it('counts a bay that has just been reported free', () => {
    const cleared = REPORTED.map((b) => (b.current_status === 'UNKNOWN' ? bay(b.slot_code, 'FREE') : b))
    expect(countSlots(cleared)).toEqual({ total: 5, occupied: 3, available: 2, unknown: 0 })
  })
})

describe('applySlotMessage', () => {
  it('updates the bay named by a real slot_occupied envelope', () => {
    const next = applySlotMessage(REPORTED, slotMessage('slot_occupied', 'A1', 'OCCUPIED'))
    expect(next).not.toBeNull()
    expect(next?.find((b) => b.slot_code === 'A1')?.current_status).toBe('OCCUPIED')
  })

  it('accepts all three event types that carry a committed reading', () => {
    for (const type of ['slot_occupied', 'slot_freed', 'slot_snapshot']) {
      const next = applySlotMessage(REPORTED, slotMessage(type, 'B3', 'FREE'))
      expect(next?.find((b) => b.slot_code === 'B3')?.current_status).toBe('FREE')
    }
  })

  it('matches on the bay code, which is what the server sends', () => {
    // The numeric id is a database detail; the socket names bays by code.
    const next = applySlotMessage(REPORTED, slotMessage('slot_freed', 'B1', 'FREE'))
    expect(next?.find((b) => b.slot_code === 'B1')?.current_status).toBe('FREE')
    expect(next?.find((b) => b.slot_code === 'A2')?.current_status).toBe('OCCUPIED')
  })

  it('leaves the list untouched for a message about something else', () => {
    const others: WSMessage[] = [
      { type: 'summary', channel: 'summary', payload: { total: 5, occupied: 3, available: 0, unknown: 2 } },
      { type: 'vehicle_entered', channel: 'session', payload: {} },
      { type: 'hello' },
    ]
    for (const msg of others) expect(applySlotMessage(REPORTED, msg)).toBeNull()
  })

  it('ignores a bay this dashboard is not showing', () => {
    expect(applySlotMessage(REPORTED, slotMessage('slot_freed', 'Z9', 'FREE'))).toBeNull()
  })

  it('ignores a malformed slot message instead of guessing', () => {
    expect(applySlotMessage(REPORTED, { type: 'slot_freed', channel: 'slot', payload: {} })).toBeNull()
    expect(applySlotMessage(REPORTED, { type: 'slot_freed', channel: 'slot' })).toBeNull()
  })

  it('reports no change when the bay already reads that way', () => {
    expect(applySlotMessage(REPORTED, slotMessage('slot_occupied', 'A2', 'OCCUPIED'))).toBeNull()
  })

  it('does not mutate the list it was given', () => {
    const before = REPORTED.map((b) => b.current_status)
    applySlotMessage(REPORTED, slotMessage('slot_freed', 'A2', 'FREE'))
    expect(REPORTED.map((b) => b.current_status)).toEqual(before)
  })

  it('ignores the shape this client used to listen for', () => {
    // `{type: 'SLOT_UPDATE', slot_id: 3}` was never on the wire, which is why
    // live updates silently did nothing.
    const fictional = { type: 'SLOT_UPDATE', slot_id: 3, status: 'FREE' } as unknown as WSMessage
    expect(applySlotMessage(REPORTED, fictional)).toBeNull()
  })
})
