import type { ReactNode } from 'react'
import { RefreshCw } from 'lucide-react'
import type { Freshness } from '../lib/freshness'
import { clockOf, timeAgo } from '../lib/time'
import { Chip } from './ui/chip'
import type { Tone } from './ui/panel'

const FACE: Record<Freshness['state'], { tone: Tone; label: string }> = {
  waiting: { tone: 'neutral', label: 'No status yet' },
  live: { tone: 'good', label: 'Live' },
  quiet: { tone: 'info', label: 'Unchanged' },
  held: { tone: 'warn', label: 'Held' },
  stale: { tone: 'bad', label: 'Behind' },
}

/** Where the numbers on this panel came from and how long ago.
 *
 * Not decoration: the pipeline finishes its pass and stops emitting, so "no update
 * for 20 minutes" is either a still lot or a dead channel, and an operator cannot
 * tell which without the age and the source stated next to the count.
 */
export function FreshnessLine({
  freshness,
  updatedAt,
  onRefresh,
}: {
  freshness: Freshness
  updatedAt: number | null
  onRefresh?: () => void
}) {
  const face = FACE[freshness.state]
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-mist-600">
      <Chip tone={face.tone} label={face.label} hint={freshness.message} pulse={freshness.state === 'live'} />
      <span>{freshness.message}</span>
      {updatedAt !== null && (
        <span className="tabular">
          as of {clockOf(updatedAt)} · {timeAgo(updatedAt)}
        </span>
      )}
      {onRefresh && (
        <button
          type="button"
          onClick={onRefresh}
          className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-2 py-1 text-mist-400 hover:text-mist-50 hover:border-volt-500/50 transition-colors"
          title="Re-read the bay list from the API"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Re-read
        </button>
      )}
    </div>
  )
}

/** A single quiet summary line for the header of a panel. */
export function MetaNote({ children }: { children: ReactNode }) {
  return <p className="text-xs text-mist-600 leading-relaxed">{children}</p>
}
