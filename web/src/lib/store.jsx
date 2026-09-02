import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken, onSignedOut } from './api'

// One signed-in account per browser. "Signed in" means Levo holds a token that
// says which public key proved itself; it never means Levo holds a key.

const Ctx = createContext(null)

const DEFAULT_CONFIG = {
  chain: '', hrp: 'tb', testnet: true, explorer_url: '',
  payment: { asset: '', label: 'USDX', decimals: 8 },
  stake: { label: 'SEQ', decimals: 8 },
  staking_floor_atoms: 4000000000000, first_tier_atoms: 4000000000000,
  first_tier_is_chain_floor: true, links: {},
}

export function StoreProvider({ children }) {
  const [standing, setStanding] = useState(null)   // /api/me
  const [tiers, setTiers] = useState(null)
  const [config, setConfig] = useState(DEFAULT_CONFIG)
  const [loading, setLoading] = useState(true)
  const [health, setHealth] = useState(null)
  const [meError, setMeError] = useState(null)

  const refresh = useCallback(async () => {
    if (!getToken()) { setStanding(null); return null }
    try {
      const s = await api.me()
      setStanding(s)
      setMeError(null)
      return s
    } catch (e) {
      if (e.status === 401) { setToken(null); setStanding(null) }
      else setMeError(e.message)
      return null
    }
  }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      const [t, c, h] = await Promise.allSettled([api.tiers(), api.config(), api.health()])
      if (!alive) return
      if (t.status === 'fulfilled') setTiers(t.value)
      if (c.status === 'fulfilled') setConfig({ ...DEFAULT_CONFIG, ...c.value })
      if (h.status === 'fulfilled') setHealth(h.value)
      else if (h.reason && h.reason.body && h.reason.body.node) setHealth(h.reason.body)
      await refresh()
      if (alive) setLoading(false)
    })()
    const off = onSignedOut(() => setStanding(null))
    return () => { alive = false; off() }
  }, [refresh])

  const signOut = useCallback(() => { setToken(null); setStanding(null) }, [])

  const explorer = useCallback((kind, value) => {
    if (!config.explorer_url || !value) return null
    return config.explorer_url + '/' + kind + '/' + value
  }, [config.explorer_url])

  const signedIn = !!standing
  const value = {
    standing, tiers, config, loading, health, meError, signedIn,
    refresh, signOut, setStanding, explorer,
    account: standing ? standing.account : null,
    tier: standing ? standing.tier : null,
    mayList: !!(standing && standing.tier && standing.tier.may_list),
    payment: config.payment,
    stake: config.stake,
    links: config.links || {},
    nodeDown: !!(health && health.node && health.node.reachable === false),
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useStore() {
  const v = useContext(Ctx)
  if (!v) throw new Error('useStore used outside StoreProvider')
  return v
}
