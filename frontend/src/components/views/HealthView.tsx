import { useQuery } from '@tanstack/react-query'
import { Activity, Database, Gauge, Server, Wifi } from 'lucide-react'
import { health } from '../../lib/api'
import { backendRows, clientRows, STATE_LABEL, type CheckRow, type CheckState } from '../../lib/health'
import { useLive } from '../../lib/live'
import { uptimeLabel, clockOf } from '../../lib/time'
import { Chip } from '../ui/chip'
import { ErrorState, LoadingState } from '../ui/states'
import { Panel, PanelHeader, SectionTitle, TONE, type Tone } from '../ui/panel'
import { StatCard } from '../ui/stat-card'

const CHECK_TONE: Record<CheckState, Tone> = {
  ok: 'good',
  warn: 'warn',
  bad: 'bad',
  unknown: 'neutral',
}

/** What the API process can prove about itself, and what this tab can prove about
 * its own connections - in two separate panels.
 *
 * Merging them is the mistake this page avoids: a wall display whose socket dropped
 * is not a broken database, and a `degraded` verdict from `/health` is not this
 * browser failing to load. Each side can be wrong while the other is fine.
 */
export function HealthView() {
  const live = useLive()
  const { data, error, isLoading, isFetching, refetch, dataUpdatedAt } = useQuery({
    queryKey: ['health'],
    queryFn: health,
    refetchInterval: 20_000,
    staleTime: 20_000,
  })

  const subject = { health: data ?? null, failed: !!error, errorText: error?.message }
  const rows = backendRows(subject)
  const mine = clientRows({ socket: live.socket, apiOk: error ? false : data ? true : null, close: live.socketClose })

  return (
    <div className="space-y-5">
      <SectionTitle
        title="System health"
        subtitle="The backend's own report, plus the two connections this browser holds: REST reads and the event channel."
        actions={
          <button
            type="button"
            onClick={() => void refetch()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-2.5 py-1.5 text-xs text-mist-400 hover:text-mist-50 hover:border-volt-500/50 transition-colors"
          >
            <Activity className="w-3.5 h-3.5" />
            {isFetching ? 'Reading' : 'Re-read'}
          </button>
        }
      />

      {data ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard label="Uptime" value={uptimeLabel(data.uptime_s)} tone={data.status === 'ok' ? 'good' : 'warn'} hint="the API process, not the machine" />
          <StatCard label="Frames read" value={data.frames_in} tone="info" hint="frames the pipeline has pushed into the frame hub" />
          <StatCard label="Events published" value={data.events_out} tone="info" hint="events the bus handed to consumers" />
          <StatCard
            label="Dropped events"
            value={data.dropped_events}
            tone={data.dropped_events > 0 ? 'warn' : 'good'}
            hint="lost before a consumer saw them, plus viewers too slow to read"
            footnote={`${data.ws_clients} browser(s) on /ws/live · ${data.ws_ticket_refusals} ticket refusal(s)`}
          />
        </div>
      ) : isLoading ? (
        <Panel>
          <LoadingState label="Reading /health…" />
        </Panel>
      ) : (
        <ErrorState error={error} detail="Nothing on this page is being asserted about the backend while the request fails." />
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel rise={false}>
          <PanelHeader
            title="Backend report"
            icon={<Server className="w-4 h-4" />}
            meta={data ? `read ${clockOf(dataUpdatedAt)}` : 'no answer'}
          />
          <CheckList rows={rows} />
          <p className="text-[0.7rem] text-mist-600 mt-3 leading-relaxed">
            `/health` is authenticated and answers from the API process itself: the database check reflects the migrated
            tables, the camera list is only the cameras this process started, and the GPU probe is what torch reports.
          </p>
        </Panel>

        <Panel rise={false}>
          <PanelHeader title="This browser" icon={<Wifi className="w-4 h-4" />} meta="connection state as the tab sees it" />
          <CheckList rows={mine} />
          <div className="mt-3 flex flex-wrap gap-2">
            <Chip tone="info" label={`${live.slots.length} bays held`} hint="rows this tab is currently rendering" />
            <Chip
              tone={live.freshness.untrustworthy ? 'warn' : 'good'}
              label={live.freshness.state === 'live' ? 'statuses current' : 'statuses may lag'}
              hint={live.freshness.message}
            />
          </div>
          <p className="text-[0.7rem] text-mist-600 mt-3 leading-relaxed">
            Two different connections: HTTP reads for the numbers, and <code>WebSocket /ws/live</code> for bay changes,
            opened with a single-use ticket minted by <code>POST /auth/ws-ticket</code>. Either can fail on its own.
          </p>
        </Panel>
      </div>

      <Panel rise={false}>
        <PanelHeader title="Pipeline counters by camera" icon={<Gauge className="w-4 h-4" />} meta="only cameras this process runs" />
        {data && data.cameras.length > 0 ? (
          <ul className="space-y-2">
            {data.cameras.map((camera) => (
              <li key={camera.camera_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-ink-950/40 px-3 py-2">
                <span className="flex items-center gap-2 min-w-0">
                  <span className={`h-2 w-2 rounded-full ${TONE[camera.streaming ? 'good' : camera.frames_available ? 'warn' : 'neutral'].dot}`} />
                  <span className="font-mono text-xs text-mist-50 truncate">{camera.camera_id}</span>
                  <span className="text-[0.68rem] text-mist-600">{camera.role}</span>
                </span>
                <span className="text-[0.68rem] text-mist-400 tabular">
                  {camera.streaming ? 'reading frames' : camera.frames_available ? 'frames held, reader idle' : 'no frames'} ·{' '}
                  {camera.frames_processed ?? 0} processed · pass {camera.passes}
                  {camera.synthetic_replay && ' · synthetic replay'}
                  {camera.error && ` · ${camera.error}`}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-mist-600 leading-relaxed">
            {data
              ? 'The API process has started no pipeline for any camera, so there is nothing streaming into the frame hub right now.'
              : 'No health answer yet.'}
          </p>
        )}
      </Panel>

      <Panel rise={false}>
        <PanelHeader title="What this page cannot tell you" icon={<Database className="w-4 h-4" />} />
        <p className="text-sm text-mist-400 leading-relaxed">
          It reports the state the server exposes, not the state of the video beyond the last frame. A camera whose file
          ended looks identical to one that was never started except in the <code>frames_processed</code> counter, and a
          database that answers cannot be said to hold correct rows by a health check alone - the test suite's
          <code> PRAGMA integrity_check</code> and Alembic version assertions are what cover that.
        </p>
      </Panel>
    </div>
  )
}

function CheckList({ rows }: { rows: CheckRow[] }) {
  return (
    <ul className="space-y-2">
      {rows.map((row) => (
        <li key={row.id} className="flex items-start gap-3 border-b border-line/70 pb-2 last:border-0">
          <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${TONE[CHECK_TONE[row.state]].dot}`} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-sm font-medium text-mist-50">{row.label}</span>
              <span className={`text-xs font-semibold ${TONE[CHECK_TONE[row.state]].text}`}>
                {row.value} · {STATE_LABEL[row.state]}
              </span>
            </div>
            <p className="text-[0.7rem] text-mist-600 leading-relaxed mt-0.5">{row.detail}</p>
          </div>
        </li>
      ))}
    </ul>
  )
}
