import type { ReactNode } from 'react'
import { AlertTriangle, Inbox, Loader2, Sparkles } from 'lucide-react'
import { ApiRequestError } from '../../lib/api'

export function describeFailure(error: unknown): { code: string | null; text: string } {
  if (error instanceof ApiRequestError) {
    return { code: error.code, text: `${error.message} (HTTP ${error.status}, ${error.code})` }
  }
  if (error instanceof Error) return { code: null, text: error.message }
  return { code: null, text: 'Something unreadable went wrong.' }
}

export function LoadingState({ label = 'Reading from the backend…' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-mist-600 py-6">
      <Loader2 className="w-4 h-4 animate-spin text-volt-400" />
      {label}
    </div>
  )
}

/** A section with nothing in it *because the lot is empty*, which is a different
 * claim from a section that failed to read. */
export function EmptyState({ title, detail, icon }: { title: string; detail?: string; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center">
      <span className="text-mist-600">{icon ?? <Inbox className="w-5 h-5" />}</span>
      <p className="text-sm font-medium text-mist-200">{title}</p>
      {detail && <p className="text-xs text-mist-600 max-w-md leading-relaxed">{detail}</p>}
    </div>
  )
}

export function ErrorState({ error, detail }: { error?: unknown; detail?: string }) {
  const found = describeFailure(error)
  return (
    <div className="flex items-start gap-2.5 rounded-xl border border-rose-500/30 bg-rose-500/8 p-3.5" role="alert">
      <AlertTriangle className="w-4 h-4 text-rose-300 mt-0.5 shrink-0" />
      <div className="text-sm leading-relaxed">
        <p className="text-rose-100 font-medium">{found.text}</p>
        {detail && <p className="text-xs text-mist-400 mt-1">{detail}</p>}
      </div>
    </div>
  )
}

/** The state a feature this project has not built gets, rather than an empty chart
 * that reads as a zero. */
export function PlannedState({
  title,
  detail,
  evidence,
}: {
  title: string
  detail: string
  /** What the server actually answers today, e.g. `501 not_implemented`. */
  evidence?: string
}) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-xl border border-volt-500/25 bg-volt-500/8 p-4">
      <div className="flex items-center gap-2 text-volt-300">
        <Sparkles className="w-4 h-4" />
        <span className="text-[0.7rem] font-semibold uppercase tracking-[0.14em]">Not implemented</span>
      </div>
      <p className="text-sm font-medium text-mist-50">{title}</p>
      <p className="text-xs text-mist-400 leading-relaxed max-w-2xl">{detail}</p>
      {evidence && (
        <p className="text-[0.7rem] text-mist-600 font-mono mt-1 border-t border-line pt-2 w-full">{evidence}</p>
      )}
    </div>
  )
}
