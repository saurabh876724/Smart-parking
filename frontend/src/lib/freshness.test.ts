import { describe, expect, it } from 'vitest'
import { QUIET_AFTER_MS, STALE_AFTER_MS, asOf, freshnessOf } from './freshness'

const NOW = 1_800_000_000_000

describe('freshnessOf', () => {
  it('says it is waiting rather than showing an empty lot as a quiet one', () => {
    const result = freshnessOf({ updatedAt: null, socket: 'live', now: NOW })
    expect(result.state).toBe('waiting')
    expect(result.ageMs).toBeNull()
    expect(result.untrustworthy).toBe(true)
  })

  it('calls the data live only while the channel is open and an update is recent', () => {
    const result = freshnessOf({ updatedAt: NOW - 1_000, socket: 'live', now: NOW })
    expect(result.state).toBe('live')
    expect(result.untrustworthy).toBe(false)
  })

  it('distinguishes a still lot from a dead channel', () => {
    const quiet = freshnessOf({ updatedAt: NOW - QUIET_AFTER_MS - 1, socket: 'live', now: NOW })
    expect(quiet.state).toBe('quiet')
    expect(quiet.message).toMatch(/no bay has changed/)
    expect(quiet.untrustworthy).toBe(false)

    const held = freshnessOf({ updatedAt: NOW - QUIET_AFTER_MS - 1, socket: 'offline', now: NOW })
    expect(held.state).toBe('held')
    expect(held.untrustworthy).toBe(true)
  })

  it('dates the numbers once nothing has updated them for the stale window', () => {
    const result = freshnessOf({ updatedAt: NOW - STALE_AFTER_MS - 1, socket: 'retrying', now: NOW })
    expect(result.state).toBe('stale')
    expect(result.untrustworthy).toBe(true)
    expect(result.ageMs).toBe(STALE_AFTER_MS + 1)
  })

  it('takes the REST read as the newest word when the socket has been silent', () => {
    const result = freshnessOf({
      updatedAt: NOW - 60_000,
      syncedAt: NOW - 500,
      socket: 'offline',
      now: NOW,
    })
    expect(result.ageMs).toBe(500)
    // A poll inside the quiet window on a closed channel is still a held picture.
    expect(result.state).toBe('held')
  })

  it('never reports a negative age for a timestamp that is ahead of this clock', () => {
    expect(freshnessOf({ updatedAt: NOW + 5_000, socket: 'live', now: NOW }).ageMs).toBe(0)
  })

  it('has a constant ordering the states depend on', () => {
    expect(QUIET_AFTER_MS).toBeLessThan(STALE_AFTER_MS)
  })
})

describe('asOf', () => {
  it('names the absence instead of printing a time of zero', () => {
    expect(asOf(null)).toBe('no data yet')
    expect(asOf(NOW)).not.toBe('no data yet')
  })
})
