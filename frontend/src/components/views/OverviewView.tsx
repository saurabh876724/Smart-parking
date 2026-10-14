import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Car, ClipboardList, HelpCircle, ParkingSquare, Activity, Users } from 'lucide-react'
import { durationMinutes, listSessions, lotSummary, plateFor } from '../../lib/api'
import { useLive } from '../../lib/live'
import { occupancyPercent, formatPercent, summaryDrift } from '../../lib/stats'
import { dateAndTime } from '../../lib/time'
import type { ViewId } from '../../lib/nav'
import { CameraSelect } from '../CameraSelect'
import { FreshnessLine } from '../FreshnessLine'
import { LayoutLegend, SlotMap } from '../SlotMap'
import { CameraPreview } from '../CameraPreview'
import { ErrorState, LoadingState } from '../ui/states'
import { Panel, PanelHeader, SectionTitle } from '../ui/panel'
import { StatCard } from '../ui/stat-card'
import { Or, Row, SessionStatusBadge, Table, Td, Th } from '../ui/table'

/** The one-glance page: every number here is a count of rows the API returned.
 *
 * Five cards because the occupancy engine has three committed states plus the
 * database's own total, and the fifth question an operator asks is how many cars
 * are in the lot right now - which is a session count, not a bay count, and comes
 * from a different table.
 */
export function OverviewView({ navigate }: { navigate: (id: ViewId) => void }) {
  const live = useLive()
  const { counts, cameraId } = live

  const { data: recent, error: recentError } = useQuery({
    queryKey: ['sessions', 'recent', 6],
    queryFn: () => listSessions({ limit: 6 }),
  })
  // The count of visits the server says are active, not of the rows on this page:
  // `total` is the whole filter and `items` is one page.
  const { data: activePage } = useQuery({
    queryKey: ['sessions', 'ACTIVE'],
    queryFn: () => listSessions({ status: 'ACTIVE', limit: 1 }),
  })
  const { data: summary } = useQuery({
    queryKey: ['lot-summary', cameraId],
    queryFn: () => lotSummary(cameraId),
    refetchInterval: 30_000,
  })

  const camera = live.cameras.find((entry) => entry.camera_id === cameraId)
  const percent = occupancyPercent(counts)
  const drift = summaryDrift({ local: counts, remote: summary })

  return (
    <div className="space-y-5">
      <SectionTitle
        title="Lot overview"
        subtitle="Occupancy as the engine commits it, the processed picture it is derived from, and the visits the gate pipeline recorded."
        actions={<CameraSelect cameras={live.cameras} value={cameraId} onChange={live.setCameraId} />}
      />

      {live.error && <ErrorState error={live.error} detail="The bay list could not be read, so the counts below are not current." />}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <StatCard label="Total bays" value={counts.total} tone="info" icon={<ParkingSquare className="w-4 h-4" />} hint={`${cameraId}: every calibrated bay the API returns`} footnote={summary ? `${summary.total} in the database` : 'database count pending'} loading={live.loading} />
        <StatCard label="Occupied" value={counts.occupied} tone="busy" icon={<Car className="w-4 h-4" />} hint="bays whose committed state is OCCUPIED" footnote={summary ? `${summary.occupied} in the database` : undefined} loading={live.loading} />
        <StatCard label="Available" value={counts.available} tone="good" icon={<Activity className="w-4 h-4" />} hint="bays whose committed state is FREE" footnote={summary ? `${summary.available} in the database` : undefined} loading={live.loading} />
        <StatCard
          label="Not reported"
          value={counts.unknown}
          tone="unknown"
          icon={<HelpCircle className="w-4 h-4" />}
          hint="UNKNOWN is not a free bay: no committed state has been written for these"
          footnote="Never counted as available"
          loading={live.loading}
        />
        <StatCard
          label="Active sessions"
          value={activePage?.total ?? null}
          tone="neutral"
          icon={<Users className="w-4 h-4" />}
          hint="GET /sessions?status=ACTIVE - the server's own count"
          footnote={activePage ? `page shows ${activePage.items.length} of ${activePage.total}` : 'count pending'}
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <FreshnessLine freshness={live.freshness} updatedAt={live.updatedAt} onRefresh={live.refresh} />
        <span className="text-xs text-mist-600">·</span>
        <span className="text-xs text-mist-400">
          Occupancy of reported bays: <span className="font-semibold text-mist-50 tabular">{formatPercent(percent)}</span>
        </span>
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        {/* Exactly one camera mount per page: two `<img>` elements on one MJPEG
            stream double the multipart connections for no extra information. */}
        <CameraPreview cameraId={cameraId} live={camera?.live} />

        <Panel rise={false} padded={false} className="overflow-hidden">
          <div className="px-4 pt-4">
            <PanelHeader
              title="Parking layout"
              icon={<ParkingSquare className="w-4 h-4" />}
              meta={`${counts.total} bays${camera?.live.streaming ? ' · camera streaming' : ' · camera not streaming'}`}
            />
          </div>
          <div className="p-4 pt-0">
            {live.slots.length === 0 ? (
              live.loading ? (
                <LoadingState label="Reading the bay list…" />
              ) : (
                <p className="text-sm text-mist-600 py-6">
                  No calibrated bays for {cameraId}. Run <code className="text-mist-400">scripts/calibrate.py</code> and
                  the occupancy engine will report this lot.
                </p>
              )
            ) : (
              <>
                <div className="rounded-xl border border-line bg-ink-950/50 p-3">
                  <SlotMap slots={live.slots} />
                </div>
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                  <LayoutLegend />
                  <p className="text-[0.68rem] text-mist-600">
                    Bay shapes come from the camera's calibration file; a bay with no committed state is drawn gray,
                    never green.
                  </p>
                </div>
              </>
            )}
          </div>
        </Panel>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
        <Panel>
          <PanelHeader title="Counts reconcile" icon={<ClipboardList className="w-4 h-4" />} meta="live read vs stored row" />
          <ul className="space-y-2.5 text-sm">
            {(
              [
                ['Total', counts.total, summary?.total],
                ['Occupied', counts.occupied, summary?.occupied],
                ['Available', counts.available, summary?.available],
                ['Not reported', counts.unknown, summary?.unknown],
              ] as const
            ).map(([label, local, remote]) => (
              <li key={label} className="flex items-center justify-between gap-3 border-b border-line/70 pb-2 last:border-0">
                <span className="text-mist-400">{label}</span>
                <span className="flex items-center gap-2 tabular">
                  <span className="font-semibold text-mist-50">{local}</span>
                  <span className="text-mist-600">/</span>
                  <span className={remote === undefined ? 'text-mist-600' : remote === local ? 'text-[#6ee7a8]' : 'text-amber-200'}>
                    {remote ?? '—'}
                  </span>
                </span>
              </li>
            ))}
          </ul>
          <p className="text-[0.7rem] text-mist-600 mt-3 leading-relaxed">{drift.note}</p>
          {summary?.from_live_frames && (
            <p className="text-[0.7rem] text-volt-300 mt-1">The summary answered from live frames rather than committed rows.</p>
          )}
        </Panel>

        <Panel rise={false} padded={false} className="overflow-hidden">
          <div className="px-4 pt-4">
            <PanelHeader
              title="Recent sessions"
              icon={<Users className="w-4 h-4" />}
              meta={recent ? `latest ${recent.items.length} of ${recent.total}` : 'loading'}
              actions={
                <button
                  type="button"
                  onClick={() => navigate('sessions')}
                  className="inline-flex items-center gap-1 text-xs text-volt-300 hover:text-volt-200 transition-colors"
                >
                  All sessions
                  <ArrowRight className="w-3.5 h-3.5" />
                </button>
              }
            />
          </div>
          {recentError ? (
            <div className="p-4 pt-0">
              <ErrorState error={recentError} />
            </div>
          ) : !recent ? (
            <div className="p-4 pt-0">
              <LoadingState label="Reading the session ledger…" />
            </div>
          ) : recent.items.length === 0 ? (
            <div className="p-4 pt-0 pb-4">
              <p className="text-sm text-mist-600 leading-relaxed">
                The ledger is empty. Sessions are written when the gate pipeline is run over a clip
                (<code className="text-mist-400">scripts/run_gate.py</code>) and persisted by the event consumer, so a
                lot that has only had its bays analysed has no visits yet.
              </p>
            </div>
          ) : (
            <Table
              head={
                <>
                  <Th>Session</Th>
                  <Th>Plate</Th>
                  <Th>Entry</Th>
                  <Th>Duration</Th>
                  <Th>Status</Th>
                </>
              }
            >
              {recent.items.map((row) => (
                <Row key={row.id}>
                  <Td className="font-mono text-xs text-mist-400 whitespace-nowrap">
                    <Or>{row.source_session_id}</Or>
                  </Td>
                  <Td className="text-mist-50 font-medium whitespace-nowrap">
                    {plateFor(row) ?? <span className="text-mist-600 italic">not read</span>}
                  </Td>
                  <Td className="text-mist-400 whitespace-nowrap tabular">
                    <Or>{row.entry_time ? dateAndTime(row.entry_time) : null}</Or>
                  </Td>
                  <Td className="tabular">
                    <Or>{durationMinutes(row.duration_seconds) === null ? null : `${durationMinutes(row.duration_seconds)} min`}</Or>
                  </Td>
                  <Td>
                    <SessionStatusBadge status={row.status} />
                  </Td>
                </Row>
              ))}
            </Table>
          )}
        </Panel>
      </div>
    </div>
  )
}
