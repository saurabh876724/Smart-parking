import type { SlotSnapshot } from '../lib/api'
import { bayTone, TONE } from './ui/panel'
import { isoToMs, timeAgo } from '../lib/time'

/** The bay list as tiles, next to the shape of the lot.
 *
 * The map answers "where", this answers "which one and since when" - and a bay
 * drawn in gray is exactly the bay an operator needs the reason for, which is what
 * the held-since column is.
 */
export function BayGrid({ slots, now }: { slots: SlotSnapshot[]; now: number }) {
  if (slots.length === 0) {
    return (
      <p className="text-sm text-mist-600">
        This camera has no calibrated bays, so there is nothing for the occupancy engine to report.
      </p>
    )
  }
  return (
    <ul className="grid gap-2.5" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(8.5rem, 1fr))' }}>
      {slots.map((slot) => {
        const tone = bayTone(slot.current_status)
        const since = isoToMs(slot.status_since)
        return (
          <li
            key={slot.id}
            className={`panel-hover rounded-xl border p-3 ${TONE[tone].chip}`}
            title={`${slot.slot_code} · ${slot.slot_type}${slot.occupied_by_track_id !== null ? ` · track ${slot.occupied_by_track_id}` : ''}`}
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-base font-semibold text-mist-50 tabular">{slot.slot_code}</span>
              {slot.slot_type !== 'REGULAR' && (
                <span className="text-[0.6rem] uppercase tracking-wider text-mist-400">{slot.slot_type}</span>
              )}
            </div>
            <div className="mt-1.5 flex items-center gap-1.5">
              <span className={`h-1.5 w-1.5 rounded-full ${TONE[tone].dot}`} />
              <span className="text-xs font-medium">{slot.current_status}</span>
            </div>
            <div className="text-[0.68rem] text-mist-600 mt-1.5 leading-snug">
              {slot.current_status === 'UNKNOWN'
                ? 'no committed state reported'
                : since === null
                  ? 'in this state since the row was written'
                  : `held ${timeAgo(since, now)}`}
              {slot.occupied_by_track_id !== null && ` · track ${slot.occupied_by_track_id}`}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
