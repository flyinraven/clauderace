import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, api, getToken, onSlowRequest, onUnauthorized, setToken } from '../api/client'
import type { User } from '../types'

interface AuthState {
  user: User | null
  loading: boolean
  serverWaking: boolean
  authError: string | null
  login: (email: string, password: string) => Promise<void>
  redeemInvite: (payload: {
    code: string
    email: string
    full_name?: string
    password: string
  }) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const [serverWaking, setServerWaking] = useState(false)
  const [authError, setAuthError] = useState<string | null>(null)

  useEffect(() => {
    const clearUser = () => setUser(null)
    onUnauthorized.add(clearUser)
    onSlowRequest.add(setServerWaking)
    return () => {
      onUnauthorized.delete(clearUser)
      onSlowRequest.delete(setServerWaking)
    }
  }, [])

  useEffect(() => {
    if (!getToken()) {
      setLoading(false)
      return
    }
    api<User>('/auth/me')
      .then(setUser)
      .catch((err) => {
        // Only a genuine rejection should sign you out. A network failure or a
        // 5xx — which on Render's free tier usually just means the instance is
        // waking up — must not discard a valid session mid-study. The client
        // already clears the token itself on a real 401.
        if (err instanceof ApiError && err.status === 401) return
        setAuthError(
          'Could not reach the server. Your session is intact — retry in a moment.',
        )
      })
      .finally(() => setLoading(false))
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const result = await api<{ access_token: string; user: User }>('/auth/login', {
      method: 'POST',
      body: { email, password },
    })
    setToken(result.access_token)
    setUser(result.user)
    setAuthError(null)
  }, [])

  const redeemInvite = useCallback(
    async (payload: { code: string; email: string; full_name?: string; password: string }) => {
      const result = await api<{ access_token: string; user: User }>('/auth/redeem-invite', {
        method: 'POST',
        body: payload,
      })
      setToken(result.access_token)
      setUser(result.user)
    },
    [],
  )

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo(
    () => ({ user, loading, serverWaking, authError, login, redeemInvite, logout }),
    [user, loading, serverWaking, authError, login, redeemInvite, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
