import type { HTMLAttributes, ReactNode } from 'react'

/** Tones are semantic, not decorative: a red thing always means the same red thing
 * across the app, and the three bay colours are the three states the FSM has. */
export type Tone = 'good' | 'busy' | 'unknown' | 'info' | 'warn' | 'bad' | 'neutral'

interface ToneFace {
  chip: string
  dot: string
  text: string
}

export const TONE: Record<Tone, ToneFace> = {
  good: { chip: 'border-bay-free/30 bg-bay-free/10 text-[#6ee7a8]', dot: 'bg-bay-free', text: 'text-[#6ee7a8]' },
  busy: { chip: 'border-bay-occupied/30 bg-bay-occupied/10 text-[#ff9aa8]', dot: 'bg-bay-occupied', text: 'text-[#ff9aa8]' },
  unknown: { chip: 'border-bay-unknown/30 bg-bay-unknown/15 text-[#c3cee0]', dot: 'bg-bay-unknown', text: 'text-[#c3cee0]' },
  info: { chip: 'border-volt-500/35 bg-volt-500/12 text-volt-300', dot: 'bg-volt-400', text: 'text-volt-300' },
  warn: { chip: 'border-amber-400/30 bg-amber-400/10 text-amber-200', dot: 'bg-amber-400', text: 'text-amber-200' },
  bad: { chip: 'border-rose-500/35 bg-rose-500/12 text-rose-200', dot: 'bg-rose-500', text: 'text-rose-200' },
  neutral: { chip: 'border-line-strong bg-ink-850 text-mist-400', dot: 'bg-mist-600', text: 'text-mist-400' },
}

export const BAY_COLOR: Record<'FREE' | 'OCCUPIED' | 'UNKNOWN', string> = {
  FREE: 'var(--color-bay-free)',
  OCCUPIED: 'var(--color-bay-occupied)',
  UNKNOWN: 'var(--color-bay-unknown)',
}

/** The status of a bay, including the one an operator must not read as spare
 * capacity. */
export function bayTone(status: string): Tone {
  if (status === 'FREE') return 'good'
  if (status === 'OCCUPIED') return 'busy'
  return 'unknown'
}

interface PanelProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode
  /** The mount motion, off for a panel that remounts on every socket event. */
  rise?: boolean
  hover?: boolean
  padded?: boolean
}

export function Panel({ children, className = '', rise = true, hover = false, padded = true, ...rest }: PanelProps) {
  return (
    <section
      {...rest}
      className={`panel ${padded ? 'p-5' : ''} ${hover ? 'panel-hover' : ''} ${rise ? 'rise' : ''} ${className}`}
    >
      {children}
    </section>
  )
}

export function PanelHeader({
  title,
  icon,
  meta,
  actions,
}: {
  title: ReactNode
  icon?: ReactNode
  meta?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-3 mb-4">
      <div className="flex items-center gap-2.5 min-w-0">
        {icon && <span className="text-volt-400 shrink-0 inline-flex">{icon}</span>}
        <h2 className="text-[0.98rem] font-semibold text-mist-50 tracking-tight truncate">{title}</h2>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        {meta && <span className="text-xs text-mist-600">{meta}</span>}
        {actions}
      </div>
    </header>
  )
}

export function SectionTitle({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
      <div className="min-w-0">
        <h1 className="text-xl sm:text-2xl font-semibold text-mist-50 tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-mist-400 mt-1 max-w-3xl leading-relaxed">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 flex-wrap">{actions}</div>}
    </div>
  )
}
