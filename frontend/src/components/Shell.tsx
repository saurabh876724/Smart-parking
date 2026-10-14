import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Boxes,
  ChartLine,
  ClipboardList,
  Cctv,
  Car,
  HeartPulse,
  LogOut,
  Menu,
  Presentation,
  Radar,
  Settings,
  Wallet,
  Workflow,
  X,
  type LucideIcon,
} from 'lucide-react'
import { fetchMe, health } from '../lib/api'
import { useAuth } from '../lib/auth'
import { useLive } from '../lib/live'
import { navGroups, navItem, type ViewId } from '../lib/nav'
import { clockOf, dateLabel } from '../lib/time'
import { useNow } from '../lib/useNow'
import { Chip, PlannedBadge, SocketChip } from './ui/chip'
import type { Tone } from './ui/panel'

const ICONS: Record<ViewId, LucideIcon> = {
  overview: Radar,
  live: Radar,
  cameras: Cctv,
  sessions: ClipboardList,
  analytics: ChartLine,
  revenue: Wallet,
  health: HeartPulse,
  architecture: Boxes,
  flow: Workflow,
  settings: Settings,
  exam: Presentation,
}

/** 30 s: long enough that a page full of charts does not lean on the API, short
 * enough that a container restart shows up before an operator notices it another
 * way. */
const HEALTH_POLL_MS = 30_000

export function Shell({ view, navigate, children }: { view: ViewId; navigate: (id: ViewId) => void; children: ReactNode }) {
  const [drawer, setDrawer] = useState(false)
  const { logout } = useAuth()
  const live = useLive()
  const now = useNow()

  const { data: profile } = useQuery({ queryKey: ['me'], queryFn: fetchMe, staleTime: 300_000 })

  useEffect(() => {
    // A page change is a new place to look: put the scroll back at the top of it.
    window.scrollTo({ top: 0 })
    setDrawer(false)
  }, [view])

  const item = navItem(view)

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[16rem_minmax(0,1fr)]">
      <aside className="hidden lg:flex flex-col border-r border-line bg-ink-900/70 backdrop-blur-sm sticky top-0 h-screen">
        <SidebarContent view={view} navigate={navigate} profileEmail={profile?.email} profileRole={profile?.role} onLogout={logout} />
      </aside>

      {drawer && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            aria-label="Close the menu"
            onClick={() => setDrawer(false)}
            className="absolute inset-0 bg-ink-950/70 backdrop-blur-sm"
          />
          <aside className="relative h-full w-[17rem] max-w-[86vw] border-r border-line bg-ink-900 p-4 overflow-y-auto">
            <button
              type="button"
              onClick={() => setDrawer(false)}
              className="absolute right-3 top-3 p-1.5 text-mist-600 hover:text-mist-50"
              aria-label="Close the menu"
            >
              <X className="w-4 h-4" />
            </button>
            <SidebarContent view={view} navigate={navigate} profileEmail={profile?.email} profileRole={profile?.role} onLogout={logout} />
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-30 border-b border-line bg-ink-950/85 backdrop-blur-md">
          <div className="flex items-center gap-3 px-4 sm:px-6 py-3">
            <button
              type="button"
              onClick={() => setDrawer(true)}
              className="lg:hidden p-1.5 text-mist-400 hover:text-mist-50"
              aria-label="Open the menu"
            >
              <Menu className="w-5 h-5" />
            </button>

            <div className="min-w-0 lg:flex-1">
              <p className="text-[0.95rem] font-semibold text-mist-50 leading-tight truncate">{item.label}</p>
              <p className="text-[0.7rem] text-mist-600 truncate">{item.hint}</p>
            </div>

            <div className="hidden xl:flex flex-col items-end leading-tight mr-1">
              <span className="text-sm font-medium text-mist-50 tabular">{clockOf(now)}</span>
              <span className="text-[0.68rem] text-mist-600">{dateLabel(now)}</span>
            </div>

            <BackendChip />
            <SocketChip status={live.socket} reason={live.socketClose?.reason} />

            <div className="hidden sm:flex items-center gap-2 pl-2 border-l border-line">
              <span className="grid place-items-center w-7 h-7 rounded-lg bg-volt-500/15 border border-volt-500/30 text-[0.7rem] font-semibold text-volt-300 uppercase">
                {(profile?.email ?? 'op').slice(0, 2)}
              </span>
              <div className="max-w-[9rem]">
                <p className="text-[0.75rem] font-medium text-mist-200 truncate">{profile?.email ?? 'Signed in'}</p>
                <p className="text-[0.65rem] text-mist-600 truncate">{profile?.role ?? 'role from token'}</p>
              </div>
            </div>

            <button
              type="button"
              onClick={() => void logout()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-2.5 py-1.5 text-xs text-mist-400 hover:text-mist-50 hover:border-rose-500/40 transition-colors"
              title="Sign out of this browser and ask the server to withdraw its tokens"
            >
              <LogOut className="w-3.5 h-3.5" />
              <span className="hidden md:inline">Sign out</span>
            </button>
          </div>
        </header>

        <main className="px-4 sm:px-6 py-5 sm:py-6 flex-1 min-w-0">{children}</main>
      </div>
    </div>
  )
}

function SidebarContent({
  view,
  navigate,
  profileEmail,
  profileRole,
  onLogout,
}: {
  view: ViewId
  navigate: (id: ViewId) => void
  profileEmail?: string
  profileRole?: string
  onLogout: () => Promise<void>
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2.5 px-1 pb-5 mb-2 border-b border-line">
        <span className="grid place-items-center w-9 h-9 rounded-xl bg-gradient-to-br from-volt-500 to-volt-700 shadow-md shadow-volt-700/30">
          <Car className="w-5 h-5 text-mist-50" />
        </span>
        <div className="min-w-0">
          <p className="text-sm font-semibold text-mist-50 tracking-tight leading-tight">SmartPark AI</p>
          <p className="text-[0.66rem] text-mist-600 leading-tight">Occupancy &amp; session vision</p>
        </div>
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto py-1 pr-1">
        {navGroups().map((group) => (
          <div key={group.group}>
            <p className="text-[0.62rem] uppercase tracking-[0.16em] text-mist-600 font-semibold px-2 mb-1.5">
              {group.label}
            </p>
            <ul className="space-y-0.5">
              {group.items.map((entry) => {
                const Icon = ICONS[entry.id]
                const active = entry.id === view
                return (
                  <li key={entry.id}>
                    <button
                      type="button"
                      onClick={() => navigate(entry.id)}
                      aria-current={active ? 'page' : undefined}
                      className={`nav-item w-full text-left ${active ? 'nav-item-active' : ''}`}
                      title={entry.hint}
                    >
                      <Icon className={`w-4 h-4 shrink-0 ${active ? 'text-volt-300' : 'text-mist-600'}`} />
                      <span className="flex-1 truncate">{entry.label}</span>
                      {entry.available === 'planned' && <PlannedBadge />}
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="pt-4 mt-3 border-t border-line text-[0.68rem] text-mist-600 space-y-1">
        <p className="truncate">{profileEmail ?? 'Not signed in'}</p>
        <p>{profileRole ? `Role ${profileRole}` : 'Signed in'}</p>
        <button
          type="button"
          onClick={() => void onLogout()}
          className="lg:hidden mt-2 inline-flex items-center gap-1.5 text-mist-400 hover:text-mist-50"
        >
          <LogOut className="w-3.5 h-3.5" />
          Sign out
        </button>
      </div>
    </div>
  )
}

/** Whether *this browser* can read the API, and what the API says about itself.
 *
 * Both halves are shown because they answer different questions: a failed request
 * is a network or session problem at the desk, while `degraded` is the server's own
 * verdict on its database and cameras. Neither one is the socket state above it.
 */
function BackendChip() {
  const { data, error, isFetching } = useQuery({
    queryKey: ['health'],
    queryFn: health,
    refetchInterval: HEALTH_POLL_MS,
    staleTime: HEALTH_POLL_MS,
  })

  if (error) {
    return <Chip tone="bad" label="API unreachable" hint="this browser could not read /health; the server may be down or the session refused" />
  }
  if (!data) {
    return <Chip tone={isFetching ? 'warn' : 'neutral'} label={isFetching ? 'Checking API' : 'API unknown'} hint="the health request has not answered yet" />
  }
  const tone: Tone = data.status === 'ok' ? 'good' : 'warn'
  return (
    <Chip
      tone={tone}
      label={data.status === 'ok' ? 'Backend ok' : 'Backend degraded'}
      pulse={data.status === 'ok'}
      hint={`database: ${data.database}; ${data.cameras.filter((c) => c.streaming || c.frames_available).length} camera(s) producing frames`}
    />
  )
}
