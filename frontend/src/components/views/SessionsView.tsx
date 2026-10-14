import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Filter, ShieldCheck, ShieldX } from 'lucide-react'
import { fetchMe, listSessions } from '../../lib/api'
import type { SessionStatus } from '../../lib/types'
import { useLive } from '../../lib/live'
import { SessionsTable } from '../SessionsTable'
import { Chip } from '../ui/chip'
import { ErrorState, LoadingState } from '../ui/states'
import { Panel, PanelHeader, SectionTitle } from '../ui/panel'
import { MetaNote } from '../FreshnessLine'

const STATUSES: (SessionStatus | 'ALL')[] = ['ALL', 'ACTIVE', 'COMPLETED', 'NEEDS_REVIEW', 'ANOMALY']
const PAGE_SIZE = 25

/** The recorded ledger, paged and filtered exactly as `GET /sessions` takes it.
 *
 * `total` is the count of the filter and `items` is one page, so the footer says
 * both - a table that showed "25 sessions" for a filter matching four hundred was
 * reporting its own row count as the answer.
 */
export function SessionsView() {
  const live = useLive()
  const [status, setStatus] = useState<SessionStatus | 'ALL'>('ALL')
  const [cameraId, setCameraId] = useState('')
  const [plate, setPlate] = useState('')
  const [offset, setOffset] = useState(0)

  const { data: profile } = useQuery({ queryKey: ['me'], queryFn: fetchMe, staleTime: 300_000 })

  const { data, error, isLoading, isFetching } = useQuery({
    queryKey: ['sessions', 'page', { status, cameraId, plate, offset }],
    queryFn: () =>
      listSessions({
        limit: PAGE_SIZE,
        offset,
        ...(status === 'ALL' ? {} : { status }),
        ...(cameraId ? { camera_id: cameraId } : {}),
        ...(plate ? { plate } : {}),
      }),
    placeholderData: (previous) => previous,
  })

  const cameras = live.cameras.map((entry) => entry.camera_id)

  function reset(next: () => void) {
    next()
    setOffset(0)
  }

  return (
    <div className="space-y-5">
      <SectionTitle
        title="Parking sessions"
        subtitle="Visits the gate pipeline recorded and the persistence consumer wrote, one row per entry-to-exit pair."
        actions={
          profile ? (
            <Chip
              tone={profile.reveal_plates ? 'warn' : 'good'}
              label={profile.reveal_plates ? 'Full plates visible' : 'Plates masked'}
              icon={profile.reveal_plates ? <ShieldX className="w-3 h-3" /> : <ShieldCheck className="w-3 h-3" />}
              hint={
                profile.reveal_plates
                  ? `Role ${profile.role} is allowed to reveal plate numbers; the API sends them in full.`
                  : `Role ${profile.role} receives the masked form only - the server never sends the number.`
              }
            />
          ) : null
        }
      />

      <Panel rise={false}>
        <PanelHeader
          title="Filters"
          icon={<Filter className="w-4 h-4" />}
          meta={data ? `${data.total} matching visit(s)` : isFetching ? 'querying' : ''}
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="block">
            <span className="text-[0.68rem] uppercase tracking-[0.12em] text-mist-600 font-semibold">Status</span>
            <select
              value={status}
              onChange={(event) => reset(() => setStatus(event.target.value as SessionStatus | 'ALL'))}
              className="field mt-1.5 w-full px-2.5 py-1.5 text-sm"
            >
              {STATUSES.map((entry) => (
                <option key={entry} value={entry} className="bg-ink-900">
                  {entry === 'ALL' ? 'Any status' : entry}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="text-[0.68rem] uppercase tracking-[0.12em] text-mist-600 font-semibold">Camera</span>
            <select
              value={cameraId}
              onChange={(event) => reset(() => setCameraId(event.target.value))}
              className="field mt-1.5 w-full px-2.5 py-1.5 text-sm"
            >
              <option value="" className="bg-ink-900">
                Any camera
              </option>
              {cameras.map((entry) => (
                <option key={entry} value={entry} className="bg-ink-900">
                  {entry}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="text-[0.68rem] uppercase tracking-[0.12em] text-mist-600 font-semibold">Plate</span>
            <input
              value={plate}
              onChange={(event) => reset(() => setPlate(event.target.value.trim()))}
              placeholder="exact number, or MH12**3456 masked"
              className="field mt-1.5 w-full px-2.5 py-1.5 text-sm placeholder:text-mist-600/70"
            />
          </label>

          <div className="flex flex-col justify-end gap-1.5">
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
                className="flex-1 rounded-lg border border-line-strong px-2.5 py-1.5 text-xs text-mist-400 hover:text-mist-50 disabled:opacity-40 disabled:hover:text-mist-400 transition-colors"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={!data || offset + PAGE_SIZE >= data.total}
                onClick={() => setOffset((value) => value + PAGE_SIZE)}
                className="flex-1 rounded-lg border border-line-strong px-2.5 py-1.5 text-xs text-mist-400 hover:text-mist-50 disabled:opacity-40 disabled:hover:text-mist-400 transition-colors"
              >
                Next
              </button>
            </div>
          </div>
        </div>

        <div className="mt-3">
          <MetaNote>
            A plate search that matches nothing is an empty page rather than an error, which is how the API avoids
            confirming whether a number is in the system. Sessions are written by <code>scripts/run_gate.py</code> over a
            gate clip and persisted by the event consumer - nothing here edits them.
          </MetaNote>
        </div>
      </Panel>

      <Panel rise={false} padded={false} className="overflow-hidden">
        <div className="px-4 pt-4">
          <PanelHeader
            title="Visits"
            meta={
              data
                ? `showing ${data.items.length} of ${data.total} · page starting at ${data.offset}`
                : isLoading
                  ? 'reading'
                  : ''
            }
          />
        </div>
        <div className="p-4 pt-0">
          {error ? (
            <ErrorState error={error} detail="The session query was refused; the rows below are not being shown rather than being absent." />
          ) : isLoading && !data ? (
            <LoadingState label="Reading the session ledger…" />
          ) : (
            <SessionsTable rows={data?.items ?? []} showCamera />
          )}
        </div>
      </Panel>
    </div>
  )
}
