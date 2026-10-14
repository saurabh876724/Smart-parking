import type { ReactNode } from 'react'
import { TONE, type Tone } from './panel'
import type { SocketStatus } from '../../lib/websocket'

export function Chip({
  tone = 'neutral',
  label,
  hint,
  pulse = false,
  icon,
}: {
  tone?: Tone
  label: ReactNode
  hint?: string
  pulse?: boolean
  icon?: ReactNode
}) {
  const face = TONE[tone]
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[0.7rem] font-medium leading-4 ${face.chip}`}
      title={hint}
    >
      <span className="relative flex h-2 w-2 items-center justify-center">
        {pulse && <span className={`absolute h-2 w-2 rounded-full opacity-70 heartbeat ${face.dot}`} />}
        <span className={`h-2 w-2 rounded-full ${face.dot}`} />
      </span>
      {icon}
      {label}
    </span>
  )
}

export function StatusDot({ tone = 'neutral', hint }: { tone?: Tone; hint?: string }) {
  return <span className={`inline-block h-2 w-2 rounded-full ${TONE[tone].dot}`} title={hint} aria-hidden />
}

/** A feature the project does not have yet. Named as such rather than drawn as an
 * empty chart, which would read as "zero" to anyone glancing at a demo. */
export function PlannedBadge({ label = 'Planned' }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-volt-500/30 bg-volt-500/10 px-2.5 py-1 text-[0.7rem] font-medium text-volt-300">
      <span className="h-1.5 w-1.5 rounded-full bg-volt-400" />
      {label}
    </span>
  )
}

export const SOCKET_FACE: Record<SocketStatus, { tone: Tone; label: string; hint: string }> = {
  idle: { tone: 'warn', label: 'Opening', hint: 'the /ws/live event channel has not been asked for yet' },
  connecting: { tone: 'warn', label: 'Connecting', hint: 'a one-time ticket was minted and the handshake is in flight' },
  live: { tone: 'good', label: 'Live', hint: 'bay updates are arriving on /ws/live' },
  retrying: { tone: 'warn', label: 'Reconnecting', hint: 'the channel dropped and a new ticket is being minted' },
  offline: { tone: 'bad', label: 'Not live', hint: 'the channel is closed; these numbers come from the last REST read' },
}

export function SocketChip({ status, reason }: { status: SocketStatus; reason?: string }) {
  const face = SOCKET_FACE[status]
  return (
    <Chip
      tone={face.tone}
      label={face.label}
      pulse={status === 'live'}
      hint={reason ? `${face.hint}; last close: ${reason}` : face.hint}
    />
  )
}
