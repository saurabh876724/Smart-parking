import { useLive, LiveProvider } from '../lib/live'
import { useHashRoute } from '../lib/useHashRoute'
import { Shell } from './Shell'
import { ErrorState } from './ui/states'
import { AnalyticsView } from './views/AnalyticsView'
import { ArchitectureView } from './views/ArchitectureView'
import { CamerasView } from './views/CamerasView'
import { ExamView } from './views/ExamView'
import { FlowView } from './views/FlowView'
import { HealthView } from './views/HealthView'
import { LiveView } from './views/LiveView'
import { OverviewView } from './views/OverviewView'
import { PlannedView } from './views/PlannedView'
import { SessionsView } from './views/SessionsView'
import { SettingsView } from './views/SettingsView'

/** The authenticated application: one live session, one shell, one page at a time.
 *
 * `LiveProvider` sits above the shell on purpose. The socket, the bay list and the
 * camera selection are the same for every page, so a page change cannot open a
 * second event channel or drop the state a wall display depends on.
 */
export function Dashboard() {
  return (
    <LiveProvider>
      <Pages />
    </LiveProvider>
  )
}

function Pages() {
  const [view, navigate] = useHashRoute()
  const live = useLive()

  // A bay read that fails is not a quiet lot: say so once, above whatever page is
  // open, rather than letting every page draw its own copy of the same silence.
  const banner = live.error ? (
    <div className="px-4 sm:px-6 pt-4">
      <ErrorState
        error={live.error}
        detail="The counts and the bay map on this page are whatever this browser last read successfully."
      />
    </div>
  ) : null

  return (
    <Shell view={view} navigate={navigate}>
      {banner}
      {view === 'overview' && <OverviewView navigate={navigate} />}
      {view === 'live' && <LiveView />}
      {view === 'cameras' && <CamerasView />}
      {view === 'sessions' && <SessionsView />}
      {view === 'analytics' && <AnalyticsView />}
      {view === 'revenue' && <PlannedView id="revenue" />}
      {view === 'health' && <HealthView />}
      {view === 'architecture' && <ArchitectureView />}
      {view === 'flow' && <FlowView />}
      {view === 'settings' && <SettingsView />}
      {view === 'exam' && <ExamView navigate={navigate} />}
    </Shell>
  )
}
