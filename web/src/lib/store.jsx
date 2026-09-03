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
  first_tier_is_chain_floor: true, links: {}, source_url: '',
}

export function StoreProvider({ children }) {
  const [standing, setStanding] = useState(null)   // /api/me
  const [tiers, setTiers] = useState(null)
  const [config, setConfig] = useState(DEFAULT_CONFIG)
  const [configError, setConfigError] = useState(null)
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
      // The deployment's own facts: the chain's address prefix, the ticker
      // its figures are in, how finely the payment asset divides. Without
      // them the page falls back to defaults and states things it was never
      // told -- a treasury address with the wrong prefix, a price with the
      // wrong label. One dropped request is enough, so it is said out loud
      // rather than left to look like an answer.
      if (c.status === 'fulfilled') { setConfig({ ...DEFAULT_CONFIG, ...c.value }); setConfigError(null) }
      else setConfigError((c.reason && c.reason.message) || 'it could not be read')
      if (h.status === 'fulfilled') setHealth(h.value)
      else if (h.reason && h.reason.body && h.reason.body.node) setHealth(h.reason.body)
      await refresh()
      if (alive) setLoading(false)
    })()
    // The node can go down while someone is reading, and a banner drawn once
    // at page load would go on saying everything is fine. It is cheap: one
    // request a minute, the same one an uptime check makes.
    const beat = setInterval(() => {
      api.health()
        .then((h) => alive && setHealth(h))
        .catch((e) => { if (alive && e && e.body && e.body.node) setHealth(e.body) })
    }, 60_000)
    const off = onSignedOut(() => setStanding(null))
    return () => { alive = false; clearInterval(beat); off() }
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
    configError,
    payment: config.payment,
    stake: config.stake,
    links: config.links || {},
    nodeDown: !!(health && health.node && health.node.reachable === false),
    chainHeight: (health && health.node && health.node.height) || null,
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useStore() {
  const v = useContext(Ctx)
  if (!v) throw new Error('useStore used outside StoreProvider')
  return v
}
