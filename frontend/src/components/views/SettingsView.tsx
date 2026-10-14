import { useQuery } from '@tanstack/react-query'
import { KeyRound, Layers, Server, ShieldCheck } from 'lucide-react'
import { fetchMe, health } from '../../lib/api'
import { useAuth } from '../../lib/auth'
import { useLive } from '../../lib/live'
import { dateAndTime } from '../../lib/time'
import { Chip, SocketChip } from '../ui/chip'
import { PlannedState } from '../ui/states'
import { Panel, PanelHeader, SectionTitle } from '../ui/panel'
import { Row, Table, Td, Th } from '../ui/table'

/** The facts about this session and this deployment that the UI can actually read.
 *
 * Nothing here is editable: the configuration that changes behaviour lives in
 * `configs/app.yaml` and the camera registry in the database behind an ADMIN
 * endpoint, and a form that wrote either would be a second source of truth with no
 * validation behind it. So the page reports, and says plainly why it does not
 * change anything.
 */
export function SettingsView() {
  const live = useLive()
  const { ended, logout } = useAuth()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: fetchMe, staleTime: 300_000 })
  const { data: healthData } = useQuery({ queryKey: ['health'], queryFn: health, refetchInterval: 30_000, staleTime: 30_000 })

  return (
    <div className="space-y-5">
      <SectionTitle
        title="Settings"
        subtitle="What this browser is signed in as, what the API lets it see, and where the configuration that changes behaviour actually lives."
        actions={<SocketChip status={live.socket} reason={live.socketClose?.reason} />}
      />

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel rise={false}>
          <PanelHeader title="This account" icon={<ShieldCheck className="w-4 h-4" />} />
          <dl className="space-y-2.5 text-sm">
            <Fact label="Email" value={me?.email ?? 'reading…'} />
            <Fact label="Role" value={me?.role ?? 'reading…'} />
            <Fact label="Account active" value={me ? (me.is_active ? 'yes' : 'deactivated') : 'unknown'} />
            <Fact label="Plates revealed" value={me ? (me.reveal_plates ? 'full numbers are sent' : 'masked form only') : 'unknown'} />
            <Fact label="Created" value={me?.created_at ? dateAndTime(me.created_at) : '—'} />
            <Fact label="Session ended by" value={ended ?? 'not ended - this tab is signed in'} />
          </dl>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void logout()}
              className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs font-medium text-rose-200 hover:bg-rose-500/16 transition-colors"
            >
              Sign out of this browser
            </button>
            <Chip tone="neutral" label={`${live.slots.length} bays on screen`} />
          </div>
          <p className="text-[0.7rem] text-mist-600 mt-3 leading-relaxed">
            Signing out calls <code>POST /auth/logout</code>, which withdraws both tokens for the account before this tab
            forgets them - a second window open with the same credentials stops working too.
          </p>
        </Panel>

        <Panel rise={false}>
          <PanelHeader title="Connections" icon={<Server className="w-4 h-4" />} />
          <dl className="space-y-2.5 text-sm">
            <Fact label="REST base" value={apiBase()} mono />
            <Fact label="Event channel" value="WebSocket /ws/live, one-time ticket" mono />
            <Fact label="Picture" value="GET /cameras/{id}/stream.mjpg, access token in query" mono />
            <Fact label="Bay poll" value="every 20 s, plus socket pushes in between" />
            <Fact label="Health poll" value="every 20-30 s per page" />
            <Fact
              label="Backend says"
              value={healthData ? `${healthData.status} · database ${healthData.database}` : 'no answer read yet'}
            />
          </dl>
          <p className="text-[0.7rem] text-mist-600 mt-3 leading-relaxed">
            The bundle talks to the host that served it: <code>/api</code> and <code>/ws</code> are proxied to the API by
            Vite in development and by nginx in the compose stack, so no address is baked into the build. Override with
            <code>VITE_API_BASE</code> / <code>VITE_WS_BASE</code> only when serving the bundle away from those proxies.
          </p>
        </Panel>
      </div>

      <Panel rise={false}>
        <PanelHeader title="Session lifetimes" icon={<KeyRound className="w-4 h-4" />} meta="read from configs/app.yaml" />
        <Table
          head={
            <>
              <Th>Credential</Th>
              <Th>Lifetime</Th>
              <Th>Why it is that long</Th>
            </>
          }
        >
          <Row>
            <Td className="font-mono text-xs">access token</Td>
            <Td className="tabular">900 s</Td>
            <Td className="text-mist-400 text-xs">A leaked bearer token is a brief problem.</Td>
          </Row>
          <Row>
            <Td className="font-mono text-xs">refresh token</Td>
            <Td className="tabular">43 200 s</Td>
            <Td className="text-mist-400 text-xs">One shift, without a logout mid-call. Rotates on every refresh.</Td>
          </Row>
          <Row>
            <Td className="font-mono text-xs">socket ticket</Td>
            <Td className="tabular">15 s</Td>
            <Td className="text-mist-400 text-xs">A socket URL lands in access logs and history, so it carries a spent-once credential instead of a token.</Td>
          </Row>
          <Row>
            <Td className="font-mono text-xs">login throttle</Td>
            <Td className="tabular">10 failures / 300 s</Td>
            <Td className="text-mist-400 text-xs">Per account and address, in-process.</Td>
          </Row>
        </Table>
      </Panel>

      <Panel rise={false}>
        <PanelHeader title="Configuration the UI does not touch" icon={<Layers className="w-4 h-4" />} />
        <div className="space-y-3">
          <PlannedState
            title="No editing forms"
            detail="Cameras, bays, debounce, tariffs and pipeline selection are set in configs/app.yaml, the calibration JSON under configs/slots, and the ADMIN endpoints of the API. This page deliberately has no save button: a browser form that wrote camera configuration would need the validation the server has and the audit trail a UI cannot promise."
            evidence="POST /api/v1/cameras and POST /api/v1/slots exist and are ADMIN/OPERATOR-only, audited - not wired to a form here."
          />
          <PlannedState
            title="No camera start or stop"
            detail="Which cameras stream is decided when the API process starts, from api.pipeline.cameras. Restarting a runner is an endpoint the UI does not call, so nothing on this page can begin or end a pipeline pass."
            evidence="POST /api/v1/cameras/{id}/restart is authenticated and audited; it is not exposed in this interface."
          />
        </div>
      </Panel>
    </div>
  )
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line/70 pb-2 last:border-0">
      <dt className="text-mist-600 text-xs shrink-0">{label}</dt>
      <dd className={`text-right text-mist-200 truncate ${mono ? 'font-mono text-[0.72rem]' : ''}`}>{value}</dd>
    </div>
  )
}

function apiBase(): string {
  return (import.meta.env.VITE_API_BASE ?? '').trim() || '/api/v1 (same origin, proxied)'
}
