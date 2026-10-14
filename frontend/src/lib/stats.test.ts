import { describe, expect, it } from 'vitest'
import type { DurationStats, OccupancySeries } from './api'
import type { SlotCounts } from './slots'
import {
  bucketDate,
  bucketTime,
  durationAvailable,
  formatDuration,
  formatPercent,
  hourLabel,
  isoSince,
  occupancyPercent,
  summarizeSeries,
  summaryDrift,
} from './stats'

function counts(occupied: number, available: number, unknown: number): SlotCounts {
  return { total: occupied + available + unknown, occupied, available, unknown }
}

describe('occupancyPercent', () => {
  it('divides by the bays that are actually reported, not by the lot', () => {
    // The state this project was really in: 3 occupied, 2 free, 5 never reported.
    expect(occupancyPercent(counts(3, 2, 5))).toBe(60)
  })

  it('does not turn a camera outage into spare capacity', () => {
    // All five bays unreported except one: 0 of 1 reported, not 0 of 5.
    expect(occupancyPercent(counts(0, 1, 4))).toBe(0)
    expect(occupancyPercent(counts(1, 0, 4))).toBe(100)
  })

  it('says nothing when nothing is known', () => {
    expect(occupancyPercent(counts(0, 0, 10))).toBeNull()
    expect(occupancyPercent(counts(0, 0, 0))).toBeNull()
  })

  it('is readable, and readable as a gap when there is no number', () => {
    expect(formatPercent(occupancyPercent(counts(1, 2, 0)))).toBe('33.3%')
    expect(formatPercent(null)).toBe('Not measured')
  })
})

describe('summarizeSeries', () => {
  const series = (points: { at: string; occupied: number }[], intervals: number): OccupancySeries => ({
    camera_id: 'lot_cam1',
    bucket: 'hour',
    points,
    definition: 'bays whose occupancy interval covered the sample instant',
    intervals_considered: intervals,
  })

  it('describes an empty series as empty rather than as zero occupied', () => {
    const summary = summarizeSeries(series([], 0))
    expect(summary).toMatchObject({ samples: 0, peak: 0, mean: null, hasHistory: false, firstAt: null, lastAt: null })
  })

  it('separates "no closed stays recorded" from "a flat chart"', () => {
    const flat = series(
      [
        { at: '2026-10-14T09:00:00', occupied: 0 },
        { at: '2026-10-14T10:00:00', occupied: 0 },
      ],
      0,
    )
    expect(summarizeSeries(flat).samples).toBe(2)
    expect(summarizeSeries(flat).hasHistory).toBe(false)
    expect(summarizeSeries({ ...flat, intervals_considered: 7 }).hasHistory).toBe(true)
  })

  it('takes its extremes from the points it was given', () => {
    const summary = summarizeSeries(
      series(
        [
          { at: '2026-10-14T09:00:00', occupied: 2 },
          { at: '2026-10-14T10:00:00', occupied: 5 },
          { at: '2026-10-14T11:00:00', occupied: 0 },
        ],
        12,
      ),
    )
    expect(summary.peak).toBe(5)
    expect(summary.mean).toBeCloseTo(2.3, 1)
    expect(summary.samplesWithCars).toBe(2)
    expect(summary.firstAt).toBe('2026-10-14T09:00:00')
    expect(summary.lastAt).toBe('2026-10-14T11:00:00')
  })

  it('survives a missing answer without inventing one', () => {
    expect(summarizeSeries(null).samples).toBe(0)
    expect(summarizeSeries(undefined).hasHistory).toBe(false)
  })
})

describe('duration labels', () => {
  it('keeps "never measured" apart from "very quick"', () => {
    expect(formatDuration(null)).toBe('Not measured')
    expect(formatDuration(undefined)).toBe('Not measured')
    expect(formatDuration(Number.NaN)).toBe('Not measured')
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(12)).toBe('12s')
    expect(formatDuration(95)).toBe('2m')
    expect(formatDuration(3600)).toBe('1h')
    expect(formatDuration(3725)).toBe('1h 02m')
  })

  it('refuses to call an average over no samples a zero-minute stay', () => {
    const none: DurationStats = {
      average_s: null,
      median_s: null,
      longest_s: null,
      sample_size: 0,
      arrivals_considered: 0,
      peak_hours: [],
    }
    expect(durationAvailable(none)).toBe(false)
    expect(durationAvailable({ ...none, sample_size: 3 })).toBe(true)
    expect(durationAvailable(null)).toBe(false)
  })
})

describe('bucket labels', () => {
  it('reads a naive UTC timestamp without shifting it', () => {
    expect(bucketTime('2026-10-14T09:30:00')).toBe('09:30')
    expect(bucketDate('2026-10-14T09:30:00')).toBe('10/14')
    expect(hourLabel(9)).toBe('09:00')
    expect(hourLabel(23)).toBe('23:00')
  })

  it('leaves an unreadable value visible instead of blank', () => {
    expect(bucketTime('not a time')).toBe('not a time')
    expect(bucketDate('nope')).toBe('')
  })
})

describe('isoSince', () => {
  it('produces the naive UTC form the stats endpoints compare against', () => {
    const now = Date.UTC(2026, 9, 14, 12, 0, 0)
    const sixHoursBack = isoSince(6, now)
    expect(sixHoursBack).toBe('2026-10-14T06:00:00')
    expect(sixHoursBack).not.toContain('Z')
    expect(sixHoursBack).not.toContain('+')
  })
})

describe('summaryDrift', () => {
  it('says so when the live list and the stored rows agree', () => {
    const result = summaryDrift({ local: counts(3, 2, 5), remote: { total: 10, occupied: 3, available: 2, unknown: 5 } })
    expect(result.matches).toBe(true)
    expect(result.note).toMatch(/agree/)
  })

  it('names the count that moved rather than reporting a general mismatch', () => {
    const result = summaryDrift({ local: counts(3, 2, 5), remote: { total: 10, occupied: 2, available: 2, unknown: 5 } })
    expect(result.matches).toBe(false)
    expect(result.note).toContain('occupied: 3 live vs 2 stored')
  })

  it('does not claim agreement before the database has answered', () => {
    expect(summaryDrift({ local: counts(1, 1, 0), remote: null }).matches).toBe(false)
    expect(summaryDrift({ local: counts(1, 1, 0), remote: undefined }).matches).toBe(false)
  })
})
