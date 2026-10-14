import { Panel, PanelHeader, SectionTitle } from '../ui/panel'
import { PlannedState } from '../ui/states'
import { navItem, type ViewId } from '../../lib/nav'

/** What a nav item that has no backend shows.
 *
 * An empty chart here would read as "zero" to anyone glancing at the demo, which is
 * a worse answer than saying the feature does not exist and where it would come from.
 */
export function PlannedView({ id }: { id: ViewId }) {
  const item = navItem(id)
  return (
    <div className="space-y-4">
      <SectionTitle title={item.label} subtitle={item.hint} />
      <Panel>
        <PanelHeader title={item.label} meta="planned phase" />
        {id === 'revenue' ? (
          <PlannedState
            title="Tariffs, invoices and payments are not part of this system"
            detail="The flow ends at a bay status and a session history. There is no tariff table, no payment model and no currency anywhere in the schema, so there is nothing this page could honestly draw. The endpoint refuses rather than answering an empty series, and this page quotes that refusal instead of a number."
            evidence="GET /api/v1/stats/revenue -> 501 not_implemented · api/routers/stats.py:93"
          />
        ) : (
          <PlannedState
            title="This view has no endpoint behind it yet"
            detail="The navigation keeps the item visible so the roadmap is legible, but it renders no data because none exists to render."
          />
        )}
        <p className="text-xs text-mist-600 leading-relaxed mt-4 border-t border-line pt-3">
          Everything else in the sidebar answers from a route that works today. Coverage of what is
          built - occupancy over time, stay durations, peak arrival hours - is on Analytics.
        </p>
      </Panel>
    </div>
  )
}
