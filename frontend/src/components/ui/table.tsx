import type { ReactNode, TdHTMLAttributes, ThHTMLAttributes } from 'react'
import { TONE } from './panel'
import type { SessionStatus } from '../../lib/types'

export function Table({ head, children }: { head: ReactNode; children: ReactNode }) {
  return (
    <div className="overflow-x-auto -mx-1">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-left text-[0.68rem] uppercase tracking-[0.12em] text-mist-600">{head}</tr>
        </thead>
        <tbody className="divide-y divide-line/70">{children}</tbody>
      </table>
    </div>
  )
}

export function Th({ children, className = '', ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th {...rest} className={`py-2.5 px-3 font-semibold whitespace-nowrap ${className}`}>
      {children}
    </th>
  )
}

export function Td({ children, className = '', ...rest }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td {...rest} className={`py-3 px-3 text-mist-200 align-middle ${className}`}>
      {children}
    </td>
  )
}

export function Row({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return (
    <tr
      onClick={onClick}
      className={`transition-colors ${onClick ? 'cursor-pointer' : ''} hover:bg-volt-500/6`}
    >
      {children}
    </tr>
  )
}

/** A visit's state as the session machine names it. `NEEDS_REVIEW` and `ANOMALY`
 * are the two the operator has to act on, so they are the two that stand out. */
export const SESSION_TONE: Record<SessionStatus, 'good' | 'neutral' | 'warn' | 'bad'> = {
  ACTIVE: 'good',
  COMPLETED: 'neutral',
  NEEDS_REVIEW: 'warn',
  ANOMALY: 'bad',
}

export function SessionStatusBadge({ status }: { status: string }) {
  const tone = SESSION_TONE[status as SessionStatus] ?? 'neutral'
  const face = TONE[tone]
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[0.7rem] font-medium ${face.chip}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${face.dot}`} />
      {status}
    </span>
  )
}

/** An em dash is a value the server sent as null, which is not the same as a value
 * this client failed to read - so it is styled as absent rather than as zero. */
export function Or({ children }: { children?: ReactNode }) {
  return children === null || children === undefined || children === '' ? <span className="text-mist-600">—</span> : <>{children}</>
}
