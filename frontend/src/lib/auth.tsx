import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  logout as apiLogout,
  onSessionEnd,
  setTokens,
  type SessionEndReason,
  type TokenPair,
} from './api'
import { useQueryClient } from '@tanstack/react-query'

interface AuthContextType {
  isAuthenticated: boolean
  /** Why this browser stopped being signed in, or null when it never was.
   * `LoginPage` shows it, because "your password was wrong" and "the server
   * withdrew this session" need different answers from an operator. */
  ended: SessionEndReason | null
  login: (pair: TokenPair) => void
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextType>({
  isAuthenticated: false,
  ended: null,
  login: () => {},
  logout: async () => {},
})

export function AuthProvider({ children }: { children: ReactNode }) {
  // Both tokens, or the session cannot outlive its first access token: a browser
  // that kept only the fifteen-minute one had no way back in without a reload.
  const [isAuthenticated, setIsAuthenticated] = useState(
    () => !!getAccessToken() || !!getRefreshToken(),
  )
  const [ended, setEnded] = useState<SessionEndReason | null>(null)
  const queryClient = useQueryClient()

  /** The dashboard is gone, so the data it was showing is gone with it.
   *
   * Cached rows survive an unmount in TanStack Query, and a sign-in on the same
   * browser would otherwise paint the previous account's bays for the moment it
   * takes the first fetch to answer.
   */
  const dropEverything = useCallback(() => {
    clearTokens()
    queryClient.clear()
    setIsAuthenticated(false)
  }, [queryClient])

  useEffect(() => onSessionEnd((reason) => {
    setEnded(reason)
    dropEverything()
  }), [dropEverything])

  const login = (pair: TokenPair) => {
    setTokens(pair)
    setEnded(null)
    queryClient.clear()
    setIsAuthenticated(true)
  }

  const logout = async () => {
    // The server first: "Logout" has to withdraw the credentials, not only stop
    // using them. `apiLogout` clears the tokens and reports `logout` to the
    // listener above, which is what takes the dashboard down.
    await apiLogout()
  }

  return (
    <AuthContext.Provider value={{ isAuthenticated, ended, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
