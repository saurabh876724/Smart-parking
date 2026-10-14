import { describe, expect, it } from 'vitest'
import {
  DEFAULT_VIEW,
  NAV_ITEMS,
  hashForView,
  isViewId,
  navGroups,
  navItem,
  viewFromHash,
  type ViewId,
} from './nav'

describe('viewFromHash', () => {
  it('reads the page out of the three shapes a link arrives in', () => {
    expect(viewFromHash('#/live')).toBe('live')
    expect(viewFromHash('#cameras')).toBe('cameras')
    expect(viewFromHash('#/analytics?x=1')).toBe('analytics')
  })

  it('is forgiving about case and whitespace because a typed URL is input', () => {
    expect(viewFromHash('#/Sessions')).toBe('sessions')
    expect(viewFromHash('  #/health  ')).toBe('health')
  })

  it('falls back to the overview rather than a page with no data', () => {
    expect(viewFromHash('')).toBe(DEFAULT_VIEW)
    expect(viewFromHash('#/')).toBe(DEFAULT_VIEW)
    expect(viewFromHash('#/revenue-and-billing')).toBe(DEFAULT_VIEW)
    expect(viewFromHash('#/../etc')).toBe(DEFAULT_VIEW)
  })

  it('round-trips with hashForView', () => {
    for (const item of NAV_ITEMS) {
      expect(viewFromHash(hashForView(item.id))).toBe(item.id)
    }
  })
})

describe('the navigation table', () => {
  it('names every view exactly once', () => {
    const ids = NAV_ITEMS.map((item) => item.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const id of ids) expect(isViewId(id)).toBe(true)
  })

  it('orders the sidebar as operations, insight, system', () => {
    expect(navGroups().map((group) => group.group)).toEqual(['operations', 'insight', 'system'])
  })

  it('marks revenue planned and everything else live, so the honest label is the exception', () => {
    const planned = NAV_ITEMS.filter((item) => item.available === 'planned').map((item) => item.id)
    expect(planned).toEqual(['revenue'])
  })

  it('gives each group a heading an operator can read', () => {
    expect(navGroups().map((group) => group.label)).toEqual(['Operations', 'Insight', 'System'])
    for (const group of navGroups()) {
      expect(group.items.length).toBeGreaterThan(0)
      for (const item of group.items) expect(item.hint.length).toBeGreaterThan(0)
    }
  })

  it('refuses to invent a view', () => {
    expect(isViewId('billing')).toBe(false)
    expect(() => navItem('billing' as ViewId)).toThrow(/unknown view/)
  })
})
