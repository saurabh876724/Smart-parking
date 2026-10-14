/** The sidebar's own vocabulary, kept free of React and of lucide so the routing
 * rules below can be tested in node with the rest of the pure logic.
 *
 * `available` is the part a demo cannot fake: every view here is wired to an
 * endpoint that answers today, except the ones deliberately marked `planned`,
 * which render an honest "not implemented" panel instead of an empty chart.
 */

export type ViewId =
  | 'overview'
  | 'live'
  | 'cameras'
  | 'sessions'
  | 'analytics'
  | 'revenue'
  | 'health'
  | 'architecture'
  | 'flow'
  | 'settings'
  | 'exam'

export type Availability = 'live' | 'planned'

export type NavGroup = 'operations' | 'insight' | 'system'

export interface NavItem {
  id: ViewId
  label: string
  group: NavGroup
  /** One line under the label, and the same line the page header repeats. */
  hint: string
  available: Availability
}

export const DEFAULT_VIEW: ViewId = 'overview'

export const NAV_ITEMS: readonly NavItem[] = [
  { id: 'overview', label: 'Overview', group: 'operations', hint: 'Occupancy, bays and the latest visits at one glance.', available: 'live' },
  { id: 'live', label: 'Live Parking', group: 'operations', hint: 'Bay states as the event channel reports them.', available: 'live' },
  { id: 'cameras', label: 'Camera Monitoring', group: 'operations', hint: 'The processed picture and what the pipeline says about each camera.', available: 'live' },
  { id: 'sessions', label: 'Parking Sessions', group: 'operations', hint: 'Recorded visits, filters and plate visibility.', available: 'live' },
  { id: 'analytics', label: 'Analytics', group: 'insight', hint: 'Occupancy over time and stay durations, from persisted rows.', available: 'live' },
  { id: 'revenue', label: 'Revenue & Billing', group: 'insight', hint: 'Tariffs and payments are a later phase.', available: 'planned' },
  { id: 'health', label: 'System Health', group: 'system', hint: 'Backend, database, pipeline and what this browser is connected to.', available: 'live' },
  { id: 'architecture', label: 'Project Architecture', group: 'system', hint: 'The pipeline as the source code has it, node by node.', available: 'live' },
  { id: 'flow', label: 'Working Flow', group: 'system', hint: 'One frame from the video file to a bay on this screen.', available: 'live' },
  { id: 'settings', label: 'Settings', group: 'system', hint: 'This account, the API it talks to and what it may see.', available: 'live' },
  { id: 'exam', label: 'Exam Presentation', group: 'system', hint: 'A five-minute walkthrough of the verified system.', available: 'live' },
]

const BY_ID: Map<ViewId, NavItem> = new Map(NAV_ITEMS.map((item) => [item.id, item]))

export function isViewId(value: string): value is ViewId {
  return BY_ID.has(value as ViewId)
}

export function navItem(id: ViewId): NavItem {
  const item = BY_ID.get(id)
  // Every ViewId is in the table, so a miss is a coding error rather than input.
  if (!item) throw new Error(`unknown view ${id}`)
  return item
}

export function navGroups(): { group: NavGroup; label: string; items: NavItem[] }[] {
  const order: { group: NavGroup; label: string }[] = [
    { group: 'operations', label: 'Operations' },
    { group: 'insight', label: 'Insight' },
    { group: 'system', label: 'System' },
  ]
  return order
    .map((entry) => ({ ...entry, items: NAV_ITEMS.filter((item) => item.group === entry.group) as NavItem[] }))
    .filter((entry) => entry.items.length > 0)
}

/** The hash of a URL into a view, or the default when it names nothing.
 *
 * Hand-typed and shared links are both input here, so an unrecognised fragment is
 * `overview` rather than a page with no data and no explanation.
 */
export function viewFromHash(hash: string): ViewId {
  const raw = hash
    .trim()
    .replace(/^#\/?/, '')
    .split(/[/?&]/)[0]
    .trim()
    .toLowerCase()
  return isViewId(raw) ? raw : DEFAULT_VIEW
}

export function hashForView(id: ViewId): string {
  return `#/${id}`
}
