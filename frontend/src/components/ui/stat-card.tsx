import type { ReactNode } from 'react'
import { TONE, type Tone } from './panel'

/** The number a card shows is either a real count or an honest absence; a `0` that
 * means "not measured" is how a dashboard starts inventing data. */
export function StatCard({
  label,
  value,
  tone = 'info',
  hint,
  footnote,
  loading = false,
  icon,
}: {
  label: string
  value: number | string | null
  tone?: Tone
  hint?: string
  footnote?: ReactNode
  loading?: boolean
  icon?: ReactNode
}) {
  const face = TONE[tone]
  const shown = loading ? '—' : value === null || value === undefined ? '—' : String(value)
  return (
    <div className="panel panel-hover rise p-4 sm:p-5" title={hint}>
      <div className="flex items-center gap-2 mb-3">
        <span className={`h-1.5 w-1.5 rounded-full ${face.dot}`} />
        <span className="text-[0.72rem] uppercase tracking-[0.14em] text-mist-600 font-semibold flex-1 truncate">
          {label}
        </span>
        {icon && <span className={`${face.text} opacity-70`}>{icon}</span>}
      </div>
      {/* tabular-nums: the count changes under an operator watching a socket. */}
      <div className={`text-[1.9rem] leading-none font-semibold tabular ${face.text}`}>{shown}</div>
      {footnote && <div className="text-xs text-mist-600 mt-2 leading-snug">{footnote}</div>}
    </div>
  )
}
