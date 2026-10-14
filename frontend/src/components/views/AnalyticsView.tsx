import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { ChartLine, Clock, Wallet } from 'lucide-react'
import { durationStats, occupancySeries, revenueSummary, ApiRequestError } from '../../lib/api'
import { useLive } from '../../lib/live'
import { bucketDate, bucketTime, durationAvailable, formatDuration, hourLabel, isoSince, summarizeSeries } from '../../lib/stats'
import { MetaNote } from '../FreshnessLine'
import { Chip } from '../ui/chip'
import { ErrorState, LoadingState, PlannedState } from '../ui/states'
import { Panel, PanelHeader, SectionTitle } from '../ui/panel'
import { StatCard } from '../ui/stat-card'

const WINDOWS = [
  { label: 'Last 6 h', hours: 6 },
  { label: 'Last 24 h', hours: 24 },
  { label: 'Last 7 d', hours: 168 },
] as const

/** Charts over the rows Phase 7 persisted.
 *
 * Both series read `slot_occupancy_history` and `parking_sessions`, which are
 * written when an interval *closes* - so the bay occupied at this instant is not in
 * the history chart, and the caption says so rather than letting a flat line look
 * like an empty lot.
 */
export function AnalyticsView() {
  const live = useLive()
  const [windowHours, setWindowHours] = useState<number>(24)
  const [bucket, setBucket] = useState<'hour' | 'day'>('hour')
  const [scoped, setScoped] = useState(true)

  const from = useMemo(() => isoSince(windowHours), [windowHours])
  const cameraId = scoped ? live.cameraId : undefined

  const { data: series, error: seriesError, isLoading: seriesLoading } = useQuery({
    queryKey: ['occupancy-series', { cameraId, from, bucket }],
    queryFn: () => occupancySeries({ cameraId, from, bucket }),
    refetchInterval: 60_000,
  })
  const { data: durations, error: durationsError } = useQuery({
    queryKey: ['duration-stats', { from }],
    queryFn: () => durationStats({ from }),
  })
  // Billing is a later phase: the endpoint refuses with 501 by design, and the
  // honest answer on a chart page is the refusal, not a zero-height bar.
  const { data: revenue } = useQuery({
    queryKey: ['revenue'],
    queryFn: () =>
      revenueSummary(7).catch((err) => {
        if (err instanceof ApiRequestError && err.status === 501) return null
        throw err
      }),
  })

  const summary = summarizeSeries(series)

  return (
    <div className="space-y-5">
      <SectionTitle
        title="Analytics"
        subtitle="Aggregations the API computes over persisted rows, with the sample size each one rests on."
        actions={
          <div className="flex flex-wrap items-center gap-1.5">
            {WINDOWS.map((entry) => (
              <button
                key={entry.hours}
                type="button"
                onClick={() => setWindowHours(entry.hours)}
                className={`rounded-lg border px-2.5 py-1.5 text-xs transition-colors ${
                  windowHours === entry.hours
                    ? 'border-volt-500/60 bg-volt-500/15 text-volt-300'
                    : 'border-line-strong text-mist-400 hover:text-mist-50'
                }`}
              >
                {entry.label}
              </button>
            ))}
            <button
              type="button"
              onClick={() => setScoped((on) => !on)}
              className="ml-1"
              title="Scope the occupancy series to the selected camera"
            >
              <Chip tone={scoped ? 'info' : 'neutral'} label={scoped ? `Only ${live.cameraId}` : 'All cameras'} />
            </button>
          </div>
        }
      />

      <Panel rise={false}>
        <PanelHeader
          title="Occupancy over time"
          icon={<ChartLine className="w-4 h-4" />}
          meta={series ? `${series.intervals_considered} recorded interval(s), ${bucket} buckets` : ''}
          actions={
            <div className="flex items-center gap-1.5">
              {(['hour', 'day'] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => setBucket(option)}
                  className={`rounded-lg border px-2 py-1 text-[0.7rem] transition-colors ${
                    bucket === option ? 'border-volt-500/60 bg-volt-500/15 text-volt-300' : 'border-line-strong text-mist-400 hover:text-mist-50'
                  }`}
                >
                  by {option}
                </button>
              ))}
            </div>
          }
        />

        {seriesError ? (
          <ErrorState error={seriesError} />
        ) : seriesLoading ? (
          <LoadingState label="Aggregating occupancy history…" />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3 mb-4">
              <StatCard label="Peak in window" value={summary.peak} tone="busy" hint="most bays held at one sampled instant" />
              <StatCard label="Mean occupied" value={summary.mean ?? '—'} tone="info" hint="average over the samples the endpoint returned" />
              <StatCard label="Samples with cars" value={`${summary.samplesWithCars}/${summary.samples}`} tone="neutral" hint="how many buckets of this window were not empty" />
            </div>

            {summary.samples === 0 ? (
              <p className="text-sm text-mist-600 py-6">The window returned no samples at all.</p>
            ) : (
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={series?.points ?? []} margin={{ top: 6, right: 10, bottom: 0, left: -18 }}>
                    <defs>
                      <linearGradient id="occupancyFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.45} />
                        <stop offset="100%" stopColor="#2f80ed" stopOpacity={0.04} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="#1c2b52" vertical={false} />
                    <XAxis
                      dataKey="at"
                      stroke="#64769e"
                      tick={{ fontSize: 11, fill: '#93a4c8' }}
                      tickFormatter={(value: string) => (bucket === 'day' ? bucketDate(value) : bucketTime(value))}
                      minTickGap={18}
                    />
                    <YAxis stroke="#64769e" tick={{ fontSize: 11, fill: '#93a4c8' }} allowDecimals={false} width={44} />
                    <Tooltip
                      contentStyle={{
                        background: '#0a1124',
                        border: '1px solid #26386a',
                        borderRadius: 10,
                        fontSize: 12,
                        color: '#f2f6ff',
                      }}
                      labelFormatter={(value) => String(value).replace('T', ' ').slice(0, 16)}
                      formatter={(value: unknown) => [`${value} bay(s)`, 'occupied']}
                    />
                    <Area type="monotone" dataKey="occupied" stroke="#38bdf8" strokeWidth={1.8} fill="url(#occupancyFill)" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}

            <div className="mt-3 space-y-1">
              <MetaNote>
                {series?.definition ?? 'bays whose occupancy interval covered the sample instant'} - counted from{' '}
                <code>slot_occupancy_history</code>, which is written when an interval closes. A bay occupied right now
                has no row here yet; its live state is on the overview page.
              </MetaNote>
              {!summary.hasHistory && (
                <p className="text-[0.7rem] text-amber-200">
                  No closed occupancy intervals in this window, so this line is flat because nothing has been recorded -
                  not because the lot was empty.
                </p>
              )}
              {(summary.firstAt || summary.lastAt) && (
                <MetaNote>
                  Window sampled {summary.firstAt?.replace('T', ' ').slice(0, 16)} to {summary.lastAt?.replace('T', ' ').slice(0, 16)}
                  {cameraId ? ` · camera ${cameraId}` : ' · every camera'}.
                </MetaNote>
              )}
            </div>
          </>
        )}
      </Panel>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel rise={false}>
          <PanelHeader title="Stay duration" icon={<Clock className="w-4 h-4" />} meta={durations ? `${durations.sample_size} measured visit(s)` : ''} />
          {durationsError ? (
            <ErrorState error={durationsError} />
          ) : !durations ? (
            <LoadingState label="Averaging stays…" />
          ) : durationAvailable(durations) ? (
            <>
              <div className="grid gap-3 grid-cols-3">
                <StatCard label="Average" value={formatDuration(durations.average_s)} tone="info" />
                <StatCard label="Median" value={formatDuration(durations.median_s)} tone="neutral" />
                <StatCard label="Longest" value={formatDuration(durations.longest_s)} tone="busy" />
              </div>
              <div className="mt-3">
                <MetaNote>
                  Averages over {durations.sample_size} closed visit(s); arrivals counted: {durations.arrivals_considered}.
                  {durations.peak_hours.length > 0 && (
                    <>
                      {' '}
                      Busiest hours of day:{' '}
                      {durations.peak_hours.map((entry) => `${hourLabel(entry.hour_of_day)} (${entry.arrivals})`).join(', ')}.
                    </>
                  )}
                </MetaNote>
              </div>
            </>
          ) : (
            <p className="text-sm text-mist-600 leading-relaxed">
              No visit in this window has a measured duration, so the average and median are unreported rather than zero.
              A stay is measured when the gate pipeline closes a session.
            </p>
          )}
        </Panel>

        <Panel rise={false}>
          <PanelHeader title="Arrivals by hour of day" icon={<ChartLine className="w-4 h-4" />} meta={durations ? `${durations.arrivals_considered} arrival(s) counted` : ''} />
          {durations && durations.peak_hours.length > 0 ? (
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={durations.peak_hours.map((entry) => ({ hour: hourLabel(entry.hour_of_day), arrivals: entry.arrivals }))}>
                  <CartesianGrid stroke="#1c2b52" vertical={false} />
                  <XAxis dataKey="hour" stroke="#64769e" tick={{ fontSize: 11, fill: '#93a4c8' }} />
                  <YAxis stroke="#64769e" tick={{ fontSize: 11, fill: '#93a4c8' }} allowDecimals={false} width={34} />
                  <Tooltip
                    contentStyle={{ background: '#0a1124', border: '1px solid #26386a', borderRadius: 10, fontSize: 12, color: '#f2f6ff' }}
                    cursor={{ fill: 'rgba(47,128,237,0.10)' }}
                  />
                  <Bar dataKey="arrivals" fill="#2f80ed" radius={[4, 4, 0, 0]} maxBarSize={48} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="text-sm text-mist-600 leading-relaxed">
              The window holds no recorded arrivals. This chart is the busiest hours of the day over persisted sessions, so
              it needs gate events to mean anything.
            </p>
          )}
        </Panel>
      </div>

      <Panel rise={false}>
        <PanelHeader title="Revenue" icon={<Wallet className="w-4 h-4" />} meta={revenue ? 'an answer came back' : '501 refused'} />
        <PlannedState
          title="Revenue and billing are not implemented"
          detail="There is no tariff table, no payment record and no price per hour anywhere in this project, so there is no revenue figure to chart. The API refuses the request rather than returning an empty series, because an empty series would read as zero revenue - a claim about money this system has never measured."
          evidence="GET /api/v1/stats/revenue?days=7 -> 501 not_implemented: revenue reporting needs tariffs and payments, which are a later phase (src/smart_parking/api/routers/stats.py)"
        />
      </Panel>
    </div>
  )
}
