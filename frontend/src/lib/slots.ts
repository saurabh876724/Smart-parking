import type { SlotSnapshot } from './api'
import { SLOT_EVENT_TYPES, type SlotEventPayload, type WSMessage } from './types'

export type SlotBucket = 'occupied' | 'available' | 'unknown'

/** FREE and OCCUPIED are the two states the camera measures.
 *
 * Anything else - UNKNOWN, or a debounce state that has no business reaching a
 * committed row - lands in `unknown` rather than `available`. A bay nobody has
 * confirmed empty is not a bay an operator can send a car to, and folding the
 * two together is what makes a camera outage look like spare capacity.
 */
export function classifySlotStatus(status: string): SlotBucket {
  if (status === 'OCCUPIED') return 'occupied'
  if (status === 'FREE') return 'available'
  return 'unknown'
}

export interface SlotCounts {
  total: number
  occupied: number
  available: number
  unknown: number
}

/** Counts that always satisfy total === occupied + available + unknown, so the
 * dashboard's cards add up to the lot map beside them. */
export function countSlots(slots: Pick<SlotSnapshot, 'current_status'>[]): SlotCounts {
  const counts: SlotCounts = { total: slots.length, occupied: 0, available: 0, unknown: 0 }
  for (const slot of slots) counts[classifySlotStatus(slot.current_status)] += 1
  return counts
}

/** Fold one socket message into the slot list.
 *
 * Returns the next list, or null when the message said nothing about a bay this
 * dashboard is showing - so a caller can skip the state update instead of
 * re-rendering on every event the lot emits.
 */
export function applySlotMessage(slots: SlotSnapshot[], msg: WSMessage): SlotSnapshot[] | null {
  if (msg.channel !== 'slot' || !SLOT_EVENT_TYPES.includes(msg.type)) return null
  const payload = msg.payload as SlotEventPayload | undefined
  const code = payload?.slot_id
  const status = payload?.status
  if (!code || !status) return null
  const index = slots.findIndex((slot) => slot.slot_code === code)
  if (index < 0 || slots[index].current_status === status) return null
  const next = [...slots]
  next[index] = { ...next[index], current_status: status }
  return next
}
