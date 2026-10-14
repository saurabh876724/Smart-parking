import { useState } from 'react'
import { BayGrid } from '../BayGrid'
import { CameraPreview } from '../CameraPreview'
import { CameraSelect } from '../CameraSelect'
import { FreshnessLine } from '../FreshnessLine'
import { LayoutLegend, SlotMap } from '../SlotMap'
import { Chip, SocketChip } from '../ui/chip'
import { Panel, PanelHeader, SectionTitle } from '../ui/panel'
import { StatCard } from '../ui/stat-card'
import { ErrorState } from '../ui/states'
import { useLive } from '../../lib/live'
import { useNow } from '../../lib/useNow'
import { clockOf, timeAgo } from '../../lib/time'
import { occupancyPercent, formatPercent } from '../../lib/stats'
import { Radar, Waves } from 'lucide-react'

/** The bay states as the event channel reports them, with the picture beside them.
 *
 * The two views of the same data answer different questions: the map is where a bay
 * is, the list is which one changed and when. Both are the committed status - the
 * debounce happens in the engine and is never written to a row, so a bay in
 * PENDING_OCCUPIED would still read as FREE here, which is what the FSM committed.
 */
export function LiveView() {
  const live = useLive()
  const now = useNow(1000)
  const [showMap, setShowMap] = useState(true)
  const camera = live.cameras.find((entry) => entry.camera_id === live.cameraId)

  return (
    <div className="space-y-5">
      <SectionTitle
        title="Live parking"
        subtitle="Every bay the occupancy engine has committed for this camera, updated as /ws/live delivers the change."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <CameraSelect cameras={live.cameras} value={live.cameraId} onChange={live.setCameraId} />
            <SocketChip status={live.socket} reason={live.socketClose?.reason} />
          </div>
        }
      />

      {live.error && <ErrorState error={live.error} detail="The bay list could not be read, so the tiles below may be behind the lot." />}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Occupied" value={live.counts.occupied} tone="busy" hint="committed OCCUPIED bays" />
        <StatCard label="Available" value={live.counts.available} tone="good" hint="committed FREE bays" />
        <StatCard label="Not reported" value={live.counts.unknown} tone="unknown" hint="no committed state - these are not spare bays" />
        <StatCard label="Reported occupancy" value={formatPercent(occupancyPercent(live.counts))} tone="info" hint="occupied over the bays with a committed state; UNKNOWN is excluded from the denominator" />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <FreshnessLine freshness={live.freshness} updatedAt={live.updatedAt} onRefresh={live.refresh} />
        <div className="flex items-center gap-2">
          <Chip tone="neutral" label={showMap ? 'Map + list' : 'List only'} hint="Switch the drawing off to see the whole bay list" />
          <button
            type="button"
            onClick={() => setShowMap((on) => !on)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-2.5 py-1.5 text-xs text-mist-400 hover:text-mist-50 hover:border-volt-500/50 transition-colors"
          >
            <Radar className="w-3.5 h-3.5" />
            {showMap ? 'Hide layout' : 'Show layout'}
          </button>
        </div>
      </div>

      <div className={`grid gap-5 ${showMap ? 'xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]' : ''}`}>
        {showMap && (
          <Panel rise={false}>
            <PanelHeader
              title="Bay layout"
              meta={camera?.live.streaming ? 'camera streaming' : 'camera not streaming'}
              actions={<span className="text-xs text-mist-600 tabular">{clockOf(now)}</span>}
            />
            {live.slots.length > 0 ? (
              <>
                <div className="rounded-xl border border-line bg-ink-950/50 p-3">
                  <SlotMap slots={live.slots} />
                </div>
                <div className="mt-3">
                  <LayoutLegend />
                </div>
              </>
            ) : (
              <p className="text-sm text-mist-600">No calibrated bays for this camera yet.</p>
            )}
          </Panel>
        )}

        <div className="space-y-5">
          <Panel rise={false}>
            <PanelHeader
              title="Bays"
              meta={`${live.slots.length} for ${live.cameraId}`}
              icon={<Waves className="w-4 h-4" />}
            />
            <BayGrid slots={live.slots} now={now} />
          </Panel>

          <Panel rise={false}>
            <PanelHeader
              title="Changes this tab has seen"
              meta={live.lastEventAt === null ? 'no event since the page opened' : `last at ${clockOf(live.lastEventAt)}`}
            />
            {live.recent.length === 0 ? (
              <p className="text-sm text-mist-600 leading-relaxed">
                Nothing has arrived on the channel since this page opened. The pipeline publishes a bay change when the
                FSM commits one, and after its pass over the clip ends it stays quiet, so an empty log here means the lot
                has not changed rather than that the feed is broken.
              </p>
            ) : (
              <ol className="space-y-1.5">
                {live.recent.map((change, index) => (
                  <li
                    key={`${change.at}-${index}`}
                    className="flex items-center justify-between gap-3 rounded-lg border border-line/70 bg-ink-950/40 px-3 py-1.5 text-xs"
                  >
                    <span className="flex items-center gap-2">
                      <span className="font-semibold text-mist-50 font-mono">{change.slotCode}</span>
                      <Chip
                        tone={change.status === 'FREE' ? 'good' : change.status === 'OCCUPIED' ? 'busy' : 'unknown'}
                        label={change.status}
                      />
                    </span>
                    <span className="text-mist-600 tabular">
                      {clockOf(change.at)} · {timeAgo(change.at, now)}
                      {change.trackId !== null && ` · track ${change.trackId}`}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </Panel>

          <CameraPreview cameraId={live.cameraId} live={camera?.live} />
        </div>
      </div>
    </div>
  )
}
