import { useState } from 'react'
import { useAuth } from '../lib/auth'
import { SESSION_END_MESSAGE, login as apiLogin } from '../lib/api'
import { Car, Eye, EyeOff, Lock, Mail } from 'lucide-react'

export function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const { login, ended } = useAuth()

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      // The whole pair, not the access token alone: the refresh token is what lets
      // this dashboard survive its own fifteen-minute credential.
      const pair = await apiLogin({ email, password })
      login(pair)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  // Why this screen is on: a sign-out the server withdrew, an expired session, and
  // a wrong password are three different sentences, and an operator who has just
  // been logged out of a wall they were watching needs to know which one happened.
  const notice = ended ? SESSION_END_MESSAGE[ended] : null

  return (
    <main className="min-h-screen flex items-center justify-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="flex items-center gap-3 mb-7 rise">
          <span className="grid place-items-center w-11 h-11 rounded-xl bg-gradient-to-br from-volt-500 to-volt-700 shadow-lg shadow-volt-600/25">
            <Car className="w-5 h-5 text-mist-50" />
          </span>
          <div>
            <h1 className="text-xl font-semibold text-mist-50 tracking-tight">SmartPark AI</h1>
            <p className="text-xs text-mist-600">Computer-vision parking occupancy &amp; session tracking</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="panel p-6 space-y-5 rise">
          <h2 className="text-base font-semibold text-mist-50">Sign in to the dashboard</h2>

          {notice && (
            <p className="flex items-start gap-2 rounded-lg border border-volt-500/30 bg-volt-500/10 px-3 py-2 text-sm text-volt-300">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-volt-400" />
              {notice}
            </p>
          )}

          {error && (
            <p role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-rose-500" />
              {error}
            </p>
          )}

          <label className="block">
            <span className="text-xs font-medium uppercase tracking-[0.12em] text-mist-600">Email</span>
            <span className="relative mt-1.5 block">
              <Mail className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-mist-600" />
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="username"
                placeholder="operator@lot.example"
                className="field w-full pl-10 pr-3 py-2.5 text-sm placeholder:text-mist-600/70"
              />
            </span>
          </label>

          <label className="block">
            <span className="text-xs font-medium uppercase tracking-[0.12em] text-mist-600">Password</span>
            <span className="relative mt-1.5 block">
              <Lock className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-mist-600" />
              <input
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
                placeholder="Your password"
                className="field w-full pl-10 pr-10 py-2.5 text-sm placeholder:text-mist-600/70"
              />
              <button
                type="button"
                onClick={() => setShowPassword((shown) => !shown)}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 text-mist-600 hover:text-mist-200 transition-colors"
                aria-label={showPassword ? 'Hide the password' : 'Show the password'}
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </span>
          </label>

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-gradient-to-r from-volt-600 to-volt-500 py-2.5 text-sm font-semibold text-white shadow-lg shadow-volt-700/25 transition-[filter,transform] hover:brightness-110 active:scale-[0.99] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? (
              <span className="inline-flex items-center gap-2">
                <span className="h-4 w-4 rounded-full border-2 border-white/30 border-t-white animate-spin" />
                Signing in…
              </span>
            ) : (
              'Sign in'
            )}
          </button>

          <p className="text-[0.7rem] leading-relaxed text-mist-600 border-t border-line pt-3">
            Accounts are created by <code className="text-mist-400">scripts/create_user.py</code> - this screen
            has no sign-up form. A session holds a fifteen-minute access token and a refresh token, so a
            dashboard left open across a break can extend itself instead of dead-ending.
          </p>
        </form>
      </div>
    </main>
  )
}
