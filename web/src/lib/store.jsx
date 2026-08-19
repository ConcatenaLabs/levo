import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api'

// One signed-in account per browser. "Signed in" means Levo holds a token that
// says which public key proved itself; it never means Levo holds a key.

const Ctx = createContext(null)

export function StoreProvider({ children }) {
  const [standing, setStanding] = useState(null)   // /api/me
  const [tiers, setTiers] = useState(null)
  const [loading, setLoading] = useState(true)
  const [health, setHealth] = useState(null)

  const refresh = useCallback(async () => {
    if (!getToken()) { setStanding(null); return null }
    try {
      const s = await api.me()
      setStanding(s)
      return s
    } catch (e) {
      if (e.status === 401) { setToken(null); setStanding(null) }
      return null
    }
  }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      const [t, h] = await Promise.allSettled([api.tiers(), api.health()])
      if (!alive) return
      if (t.status === 'fulfilled') setTiers(t.value)
      if (h.status === 'fulfilled') setHealth(h.value)
      await refresh()
      if (alive) setLoading(false)
    })()
    return () => { alive = false }
  }, [refresh])

  const signOut = useCallback(() => { setToken(null); setStanding(null) }, [])

  const signedIn = !!standing
  const value = {
    standing, tiers, loading, health, signedIn,
    refresh, signOut, setStanding,
    account: standing ? standing.account : null,
    tier: standing ? standing.tier : null,
    mayList: !!(standing && standing.tier && standing.tier.may_list),
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useStore() {
  const v = useContext(Ctx)
  if (!v) throw new Error('useStore used outside StoreProvider')
  return v
}
