/** One clock for the whole app.
 *
 * The backend writes naive UTC (`db/base.py`) and serialises it without an offset
 * for most fields, so a timestamp from `SlotSnapshot.status_since` is UTC while a
 * browser is not. Parsing with the explicit `Z` keeps "last change 14:03" meaning
 * the same instant on every machine in the demo.
 */

/** ISO string from the API -> a `HH:MM:SS` on this machine's clock. */
export function timeOfDay(iso: string | null | undefined): string {
  if (!iso) return '—'
  const at = parseIso(iso)
  if (at === null) return iso
  return at.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

export function dateAndTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const at = parseIso(iso)
  if (at === null) return iso
  return `${at.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })} ${at.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })}`
}

export function clockOf(now: number): string {
  const at = new Date(now)
  return at.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

export function dateLabel(now: number): string {
  return new Date(now).toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })
}

/** "just now" / "4 min ago" - a bay whose status has not moved is worth dating. */
export function timeAgo(ms: number | null, now: number = Date.now()): string {
  if (ms === null) return 'no reading yet'
  const seconds = Math.max(0, Math.round((now - ms) / 1000))
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m ago`
  return `${Math.floor(hours / 24)} d ago`
}

export function uptimeLabel(seconds: number): string {
  const total = Math.floor(seconds)
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${total % 60}s`
  return `${total}s`
}

function parseIso(iso: string): Date | null {
  // A naive UTC stamp needs its zone stated before a browser guesses one.
  const normalised = /Z$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`
  const at = new Date(normalised)
  return Number.isNaN(at.getTime()) ? null : at
}

/** ISO from the API -> epoch ms, or null when it is not a timestamp at all. */
export function isoToMs(iso: string | null | undefined): number | null {
  if (!iso) return null
  const at = parseIso(iso)
  return at === null ? null : at.getTime()
}
