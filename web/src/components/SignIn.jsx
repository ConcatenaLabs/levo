import { useState } from 'react'
import { api, setToken } from '../lib/api'
import { hasProvider, signMessage, signStakerMessage, supportsStakerSigning, connect } from '../lib/wallet'
import { useStore } from '../lib/store'

// The whole login. Ask the backend for a challenge, have the wallet sign it,
// send the signature back. The account is whatever key the signature recovers
// to, so there is nothing to register and nothing to remember.

export default function SignIn({ onDone, label = 'Sign in with your wallet' }) {
  const { refresh } = useStore()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [manual, setManual] = useState(null)   // { message } when pasting a signature
  const [pasted, setPasted] = useState('')

  async function withWallet() {
    setError(null); setBusy(true)
    try {
      const ch = await api.authChallenge()
      try { await connect() } catch { /* some wallets sign without a connect step */ }

      // Sign in AS the staking key where the wallet can, so a staker's tier is
      // there on the first screen. Signing with the master key would log the
      // same person in as a key that holds no stake, and leave them to link it
      // by hand afterwards for no reason.
      let signature
      if (await supportsStakerSigning()) {
        try {
          signature = (await signStakerMessage(ch.message)).signature
        } catch (e) {
          // A wallet that has no staking key yet is still perfectly able to
          // browse and to hold an account; fall back rather than refuse.
          signature = await signMessage(ch.message)
        }
      } else {
        signature = await signMessage(ch.message)
      }

      const r = await api.authVerify(ch.message, signature)
      setToken(r.token)
      await refresh()
      onDone && onDone(r)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  async function startManual() {
    setError(null); setBusy(true)
    try {
      const ch = await api.authChallenge()
      setManual(ch)
    } catch (e) {
      setError(e.message || String(e))
    } finally { setBusy(false) }
  }

  async function submitManual(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const r = await api.authVerify(manual.message, pasted.trim())
      setToken(r.token)
      await refresh()
      onDone && onDone(r)
    } catch (e) {
      setError(e.message || String(e))
      // The challenge is single use, so a failed attempt needs a fresh one.
      try { setManual(await api.authChallenge()) } catch {}
    } finally { setBusy(false) }
  }

  if (manual) {
    return (
      <div>
        <p className="small dim">
          Sign this exact text with your wallet, then paste the signature back.
          It authorises no payment.
        </p>
        <div className="field">
          <label>Message to sign</label>
          <textarea className="mono" readOnly value={manual.message} rows={7}
                    onFocus={(e) => e.target.select()} />
          <div className="hint">
            With the node: <span className="mono">sequentia-cli signmessage "&lt;address&gt;" "&lt;the text above&gt;"</span>
          </div>
        </div>
        <form onSubmit={submitManual}>
          <div className="field">
            <label htmlFor="sig">Signature</label>
            <input id="sig" className="mono" value={pasted} autoComplete="off"
                   onChange={(e) => setPasted(e.target.value)}
                   placeholder="base64 signature" />
          </div>
          {error && <div className="notice bad" style={{ marginBottom: '1rem' }}>{error}</div>}
          <div style={{ display: 'flex', gap: '.6rem' }}>
            <button className="btn btn-primary" disabled={busy || !pasted.trim()}>
              {busy ? 'Checking…' : 'Sign in'}
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => setManual(null)}>
              Back
            </button>
          </div>
        </form>
      </div>
    )
  }

  return (
    <div>
      {error && <div className="notice bad" style={{ marginBottom: '1rem' }}>{error}</div>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '.6rem' }}>
        {hasProvider() && (
          <button className="btn btn-primary" onClick={withWallet} disabled={busy}>
            {busy ? 'Waiting for your wallet…' : label}
          </button>
        )}
        <button className={'btn' + (hasProvider() ? ' btn-ghost' : ' btn-primary')}
                onClick={startManual} disabled={busy}>
          {hasProvider() ? 'Sign another way' : 'Sign a message to continue'}
        </button>
      </div>
      {!hasProvider() && (
        <p className="small dim" style={{ marginTop: '.9rem', marginBottom: 0 }}>
          No Sequentia wallet detected in this browser. You can still sign the
          challenge with any wallet that signs messages and paste the result.
        </p>
      )}
    </div>
  )
}
