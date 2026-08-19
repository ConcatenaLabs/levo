import { useState } from 'react'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { signMessage, hasProvider } from '../lib/wallet'
import { amount, compact, shortHex } from '../lib/format'
import SignIn from '../components/SignIn'
import Beam from '../components/Beam'

// Linking a staking key is the same primitive as signing in, aimed at a
// different key: sign a statement that names both the account and the key, and
// the stake behind that key starts counting.

function LinkKey({ onLinked }) {
  const [pubkey, setPubkey] = useState('')
  const [challenge, setChallenge] = useState(null)
  const [sig, setSig] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function start(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const ch = await api.stakeChallenge(pubkey.trim().toLowerCase())
      setChallenge(ch)
      if (hasProvider()) {
        try {
          const s = await signMessage(ch.message)
          setSig(s)
        } catch { /* the wallet may not hold this key; fall through to pasting */ }
      }
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function submit(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const st = await api.stakeLink(challenge.message, sig.trim(), pubkey.trim().toLowerCase())
      setChallenge(null); setSig(''); setPubkey('')
      onLinked(st)
    } catch (e) {
      setError(e.message)
      try { setChallenge(await api.stakeChallenge(pubkey.trim().toLowerCase())) } catch {}
    } finally { setBusy(false) }
  }

  if (!challenge) {
    return (
      <form onSubmit={start}>
        <div className="field">
          <label htmlFor="pk">Staking public key</label>
          <input id="pk" className="mono" value={pubkey} autoComplete="off"
                 onChange={(e) => setPubkey(e.target.value)}
                 placeholder="33-byte compressed key, hex" />
          <div className="hint">
            The key your stake is registered under. <span className="mono">getstakerinfo</span> lists it.
          </div>
        </div>
        {error && <div className="notice bad" style={{ marginBottom: '1rem' }}>{error}</div>}
        <button className="btn btn-primary" disabled={busy || !pubkey.trim()}>
          {busy ? 'Working…' : 'Prove I control this key'}
        </button>
      </form>
    )
  }

  return (
    <form onSubmit={submit}>
      <div className="field">
        <label>Sign this with the staking key</label>
        <textarea className="mono" readOnly rows={7} value={challenge.message}
                  onFocus={(e) => e.target.select()} />
        <div className="hint">
          <span className="mono">sequentia-cli signmessagewithprivkey "&lt;staking WIF&gt;" "&lt;the text above&gt;"</span>
        </div>
      </div>
      <div className="field">
        <label htmlFor="ssig">Signature</label>
        <input id="ssig" className="mono" value={sig} autoComplete="off"
               onChange={(e) => setSig(e.target.value)} placeholder="base64 signature" />
      </div>
      {error && <div className="notice bad" style={{ marginBottom: '1rem' }}>{error}</div>}
      <div style={{ display: 'flex', gap: '.6rem' }}>
        <button className="btn btn-primary" disabled={busy || !sig.trim()}>
          {busy ? 'Checking…' : 'Link this stake'}
        </button>
        <button type="button" className="btn btn-ghost" onClick={() => setChallenge(null)}>
          Cancel
        </button>
      </div>
    </form>
  )
}

export default function Account() {
  const { signedIn, standing, tiers, account, signOut, setStanding, refresh } = useStore()

  if (!signedIn) {
    return (
      <div className="wrap section" style={{ maxWidth: 640 }}>
        <div className="section-head">
          <h2>Sign in</h2>
          <p>
            Your account is a public key. Sign Levo's challenge and the key that
            signed it is who you are: nothing to register, no password, and
            nothing here that could spend on your behalf.
          </p>
        </div>
        <div className="card"><SignIn /></div>
      </div>
    )
  }

  const st = standing
  return (
    <div className="wrap section">
      <div className="section-head">
        <p className="eyebrow">Account</p>
        <h2>{st.tier.name}</h2>
        <p className="mono small" style={{ wordBreak: 'break-all' }}>{account}</p>
      </div>

      {tiers && (
        <div style={{ marginBottom: '2.5rem' }}>
          <Beam tiers={tiers.tiers} stakeAtoms={st.stake_atoms} />
        </div>
      )}

      <div className="grid-3" style={{ marginBottom: '2.5rem' }}>
        <div className="stat">
          <b>{compact(st.stake_atoms)}</b><span>SEQ staked</span>
        </div>
        <div className="stat">
          <b>{amount(st.tier.cap_atoms)}</b><span>USDX cap per sale</span>
        </div>
        <div className="stat">
          <b>{st.next_tier ? compact(st.to_next_atoms) : '—'}</b>
          <span>{st.next_tier ? 'SEQ to ' + st.next_tier.name : 'top tier'}</span>
        </div>
      </div>

      <div className="split">
        <div>
          <h3 style={{ marginBottom: '.75rem' }}>Staking keys</h3>
          <p className="small dim">
            Stake counts only for keys you have proven you control. One key
            counts for one account: proving it here moves it off any account
            that claimed it before.
          </p>
          {st.keys.length === 0 && (
            <div className="notice" style={{ marginBottom: '1.25rem' }}>
              No staking keys linked yet, so your stake reads as zero.
            </div>
          )}
          {st.keys.map((k) => (
            <div className="card" key={k.staker_pubkey} style={{ marginBottom: '.75rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap', alignItems: 'center' }}>
                <span className="mono small" style={{ wordBreak: 'break-all' }}>
                  {shortHex(k.staker_pubkey, 16, 10)}
                </span>
                <span className="num small">{compact(k.weight_atoms)} SEQ</span>
              </div>
              {!k.eligible_blocksigner && (
                <p className="small dim" style={{ margin: '.5rem 0 0' }}>
                  Below the chain's 40,000 SEQ blocksigner floor. It still adds
                  to your total here.
                </p>
              )}
              {k.is_login_key ? (
                <p className="small dim" style={{ margin: '.6rem 0 0' }}>
                  This is the key you signed in with, so it counts on its own.
                  Signing out is what stops it counting.
                </p>
              ) : (
                <button className="btn btn-sm btn-ghost" style={{ marginTop: '.75rem' }}
                        onClick={async () => setStanding(await api.stakeUnlink(k.staker_pubkey))}>
                  Unlink
                </button>
              )}
            </div>
          ))}
        </div>

        <div className="sticky">
          <div className="card">
            <h3>Link a staking key</h3>
            <p className="small dim">
              Prove control of the key your stake sits under, and its weight
              counts towards your tier.
            </p>
            <LinkKey onLinked={setStanding} />
          </div>
          <button className="btn btn-ghost btn-sm" style={{ marginTop: '1rem' }}
                  onClick={signOut}>Sign out</button>
        </div>
      </div>
    </div>
  )
}
