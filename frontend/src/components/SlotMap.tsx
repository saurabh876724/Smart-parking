import type { SlotSnapshot } from '../lib/api'
import { BAY_COLOR, bayTone } from './ui/panel'

interface SlotMapProps {
  slots: SlotSnapshot[]
  /** The frame the polygons were calibrated in, so the drawing is the lot's shape
   * rather than a grid of convenience. Normalised points are scaled by it. */
  width?: number
  height?: number
}

/** The calibrated bay shapes, filled with the state the engine committed.
 *
 * Bays are drawn from their polygon when the calibration has one; a camera with no
 * slots file falls back to an even grid, and the caption says which of the two the
 * operator is looking at - a grid drawn over real bay geometry would be a diagram
 * of a lot that does not exist.
 */
export function SlotMap({ slots, width = 478, height = 850 }: SlotMapProps) {
  const hasPolygons = slots.length > 0 && slots.every((slot) => (slot.polygon?.length ?? 0) >= 3)
  if (!hasPolygons) return <FallbackGrid slots={slots} width={width} height={height} />

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="w-full h-auto max-h-[68vh]"
      role="img"
      aria-label={`Parking layout for ${slots.length} bays`}
    >
      <rect x={0} y={0} width={width} height={height} fill="rgba(4,6,15,0.6)" rx={10} />
      {slots.map((slot) => {
        const tone = bayTone(slot.current_status)
        const points = slot.polygon.map((p) => `${p[0] * width},${p[1] * height}`).join(' ')
        const centroid = centreOf(slot.polygon, width, height)
        return (
          <g key={slot.id}>
            <polygon
              points={points}
              fill={BAY_COLOR[statusKey(slot.current_status)]}
              fillOpacity={tone === 'unknown' ? 0.5 : 0.85}
              stroke="var(--color-line-strong)"
              strokeWidth={1.4}
            >
              <title>{`${slot.slot_code} · ${slot.current_status}${slot.slot_type === 'REGULAR' ? '' : ` · ${slot.slot_type}`}`}</title>
            </polygon>
            <text
              x={centroid[0]}
              y={centroid[1] - 2}
              textAnchor="middle"
              className="font-semibold"
              fontSize={Math.max(11, width / 34)}
              fill={tone === 'unknown' ? '#e6ecf8' : '#06101f'}
            >
              {slot.slot_code}
            </text>
            <text
              x={centroid[0]}
              y={centroid[1] + Math.max(11, width / 30)}
              textAnchor="middle"
              fontSize={Math.max(9, width / 46)}
              fill={tone === 'unknown' ? '#e6ecf8' : '#06101f'}
            >
              {slot.current_status}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

function statusKey(status: string): 'FREE' | 'OCCUPIED' | 'UNKNOWN' {
  if (status === 'FREE') return 'FREE'
  if (status === 'OCCUPIED') return 'OCCUPIED'
  return 'UNKNOWN'
}

function centreOf(polygon: number[][], width: number, height: number): [number, number] {
  const xs = polygon.map((p) => p[0] * width)
  const ys = polygon.map((p) => p[1] * height)
  return [xs.reduce((a, b) => a + b, 0) / xs.length, ys.reduce((a, b) => a + b, 0) / ys.length]
}

function FallbackGrid({ slots, width, height }: { slots: SlotSnapshot[]; width: number; height: number }) {
  const cols = Math.max(1, Math.ceil(Math.sqrt(slots.length)))
  const rows = Math.max(1, Math.ceil(slots.length / cols))
  const cellWidth = width / cols
  const cellHeight = height / rows
  const pad = Math.min(cellWidth, cellHeight) * 0.06
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto max-h-[68vh]" role="img" aria-label={`Bay list for ${slots.length} bays`}>
      {slots.map((slot, index) => {
        const x = (index % cols) * cellWidth + pad
        const y = Math.floor(index / cols) * cellHeight + pad
        const w = cellWidth - pad * 2
        const h = cellHeight - pad * 2
        const tone = bayTone(slot.current_status)
        return (
          <g key={slot.id}>
            <rect
              x={x}
              y={y}
              width={w}
              height={h}
              rx={8}
              fill={BAY_COLOR[statusKey(slot.current_status)]}
              fillOpacity={tone === 'unknown' ? 0.5 : 0.85}
              stroke="var(--color-line-strong)"
            >
              <title>{`${slot.slot_code} · ${slot.current_status}`}</title>
            </rect>
            <text x={x + w / 2} y={y + h / 2} textAnchor="middle" fontSize={Math.max(11, width / 34)} fill="#06101f" className="font-semibold">
              {slot.slot_code}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

/** The three colours plus the fourth thing a map needs: what each one claims. */
export function LayoutLegend() {
  const entries: { label: string; meaning: string; color: string }[] = [
    { label: 'FREE', meaning: 'the engine committed an empty bay', color: BAY_COLOR.FREE },
    { label: 'OCCUPIED', meaning: 'a vehicle is held in the bay', color: BAY_COLOR.OCCUPIED },
    { label: 'UNKNOWN', meaning: 'no committed state - not a spare bay', color: BAY_COLOR.UNKNOWN },
  ]
  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-2 text-xs">
      {entries.map((entry) => (
        <li key={entry.label} className="flex items-center gap-2">
          <span className="h-3 w-3 rounded-[3px] border border-line-strong" style={{ background: entry.color }} />
          <span className="font-semibold text-mist-200">{entry.label}</span>
          <span className="text-mist-600">{entry.meaning}</span>
        </li>
      ))}
    </ul>
  )
}
