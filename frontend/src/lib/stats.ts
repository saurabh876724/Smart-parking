/** Arithmetic for the analytics and overview cards.
 *
 * Everything here is pure so the rules the brief cares about - never divide by a
 * total that includes unreported bays, never report an average over zero samples
 * as zero - are testable rather than visible only in a rendered chart.
 */

import type { DurationStats, OccupancySeries } from './api'
import type { SlotCounts } from './slots'

/** Percent of the bays whose state is measured that are occupied.
 *
 * UNKNOWN is excluded from the denominator on purpose: a bay the camera is not
 * reporting is not a free bay, and folding it into the divisor would turn a camera
 * outage into spare capacity. Null means nothing is known at all.
 */
export function occupancyPercent(counts: Pick<SlotCounts, 'occupied' | 'available'>): number | null {
  const committed = counts.occupied + counts.available
  if (committed === 0) return null
  return Math.round((counts.occupied / committed) * 1000) / 10
}

export function formatPercent(value: number | null): string {
  return value === null ? 'Not measured' : `${value.toFixed(1)}%`
}

/** The three questions a history chart can answer about itself.
 *
 * `intervals_considered` is the server's own denominator: when it is zero the
 * series is legitimately flat rather than broken, and the difference is the
 * difference between "this lot has no closed stays recorded yet" and a bug.
 */
export interface SeriesSummary {
  samples: number
  peak: number
  mean: number | null
  samplesWithCars: number
  hasHistory: boolean
  firstAt: string | null
  lastAt: string | null
}

export function summarizeSeries(series: OccupancySeries | null | undefined): SeriesSummary {
  const points = series?.points ?? []
  const occupied = points.map((p) => p.occupied)
  const peak = occupied.length ? Math.max(...occupied) : 0
  return {
    samples: points.length,
    peak,
    mean: occupied.length ? Math.round((occupied.reduce((a, b) => a + b, 0) / occupied.length) * 10) / 10 : null,
    samplesWithCars: occupied.filter((n) => n > 0).length,
    hasHistory: (series?.intervals_considered ?? 0) > 0,
    firstAt: points.length ? points[0].at : null,
    lastAt: points.length ? points[points.length - 1].at : null,
  }
}

/** Occupancy history is bucketed, and a sample's timestamp is naive UTC from the
 * server; reading it back needs the same assumption or every chart is five hours
 * off. */
export function bucketTime(iso: string): string {
  const stamp = /(\d{2}):(\d{2})/.exec(iso)
  if (!stamp) return iso
  const hour = Number(stamp[1])
  return Number.isNaN(hour) ? stamp[0] : `${String(hour).padStart(2, '0')}:${stamp[2]}`
}

export function bucketDate(iso: string): string {
  const match = /^\d{4}-\d{2}-\d{2}/.exec(iso)
  return match ? match[0].slice(5).replace('-', '/') : ''
}

export function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, '0')}:00`
}

/** A stay as the operator reads it. Null is "never measured", and 0 is a real
 * visit that lasted seconds - the two must not share a string. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return 'Not measured'
  const total = Math.round(seconds)
  if (total < 60) return `${total}s`
  const minutes = Math.round(total / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours}h` : `${hours}h ${String(rest).padStart(2, '0')}m`
}

/** Hours ago, as an ISO string the stats endpoints take (`from=`). */
export function isoSince(hours: number, now: number = Date.now()): string {
  const date = new Date(now - hours * 3_600_000)
  // The backend compares against naive UTC rows, so the parameter must not carry
  // an offset that would make the same instant mean two things.
  return date.toISOString().slice(0, 19)
}

/** What the duration panel can honestly claim, given how few visits it counted. */
export function durationAvailable(stats: DurationStats | null | undefined): boolean {
  return (stats?.sample_size ?? 0) > 0
}

/** Cross-check the live bay list against the database's own counts.
 *
 * Both are on the overview page, and when they disagree the screen has to say so
 * instead of letting an operator pick whichever looks right. */
export function summaryDrift(args: {
  local: SlotCounts
  remote: { total: number; occupied: number; available: number; unknown: number } | null | undefined
}): { matches: boolean; note: string } {
  if (!args.remote) return { matches: false, note: 'No database summary to compare against yet.' }
  const diffs: string[] = []
  const fields = ['total', 'occupied', 'available', 'unknown'] as const
  for (const field of fields) {
    if (args.local[field] !== args.remote[field]) diffs.push(`${field}: ${args.local[field]} live vs ${args.remote[field]} stored`)
  }
  return diffs.length === 0
    ? { matches: true, note: 'The socket, the bay list and the database agree on all four counts.' }
    : { matches: false, note: `Live and stored counts differ - ${diffs.join(', ')}. A bay can change between the two reads.` }
}
