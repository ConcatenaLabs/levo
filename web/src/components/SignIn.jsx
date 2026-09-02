import { useState } from 'react'
import { api, setToken } from '../lib/api'
import { hasProvider, signMessage, signStakerMessage, supportsStakerSigning, connect,
         noStakingKey, friendly } from '../lib/wallet'
import { useStore } from '../lib/store'
import { Copy, Notice } from './ui'

// The whole login. Ask the backend for a challenge, have the wallet sign it,
// send the signature back. The account is whatever key the signature recovers
// to, so there is nothing to register and nothing to remember.

export default function SignIn({ onDone, label = 'Sign in with your wallet' }) {
  const { refresh } = useStore()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [manual, setManual] = useState(null)   // { message } when pasting a signature
  const [pasted, setPasted] = useState('')
  const [address, setAddress] = useState('')

  async function withWallet() {
    setError(null); setBusy(true)
    try {
      // Connect (which doubles as the unlock prompt) before the challenge is
      // issued, so a slow approval does not eat the challenge's lifetime.
      try { await connect() } catch (e) { throw new Error(friendly(e)) }
      const staker = await supportsStakerSigning()
      const ch = await api.authChallenge()

      // Sign in AS the staking key where the wallet can, so a staker's tier is
      // there on the first screen. Signing with the master key would log the
      // same person in as a key that holds no stake, and leave them to link it
      // by hand afterwards for no reason.
      let signature
      if (staker) {
        try {
          signature = (await signStakerMessage(ch.message)).signature
        } catch (e) {
          // A wallet that has no staking key yet is still perfectly able to
          // browse and to hold an account; fall back rather than refuse. A
          // declined prompt is not that: it ends the attempt.
          if (!noStakingKey(e)) throw new Error(friendly(e))
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
      setError(friendly(e))
    } finally {
      setBusy(false)
    }
  }

  async function startManual() {
    setError(null); setBusy(true)
    try {
      setManual(await api.authChallenge())
    } catch (e) {
      setError(e.message || String(e))
    } finally { setBusy(false) }
  }

  async function submitManual(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const r = await api.authVerify(manual.message, pasted.trim(), address.trim() || undefined)
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
    const rows = manual.message.split('\n').length + 1
    return (
      <div>
        <p className="small dim">
          Sign this exact text with your wallet, then paste the signature back.
          It authorises no payment. The text has no trailing newline, so a copy
          from this box and a file read with <span className="mono">$(cat file)</span> are
          the same bytes.
        </p>
        <div className="field">
          <label htmlFor="challenge">Message to sign <Copy value={manual.message} label="Copy the message" /></label>
          <textarea id="challenge" className="mono fit" readOnly value={manual.message} rows={rows}
                    onFocus={(e) => e.target.select()} />
          <div className="hint">
            With a node, message signing needs a legacy address:{' '}
            <span className="mono">sequentia-cli getnewaddress "" legacy</span>, then{' '}
            <span className="mono">sequentia-cli signmessage "&lt;that address&gt;" "$(cat levo-challenge.txt)"</span>.
            To sign in as your staking key, so your tier shows at once:{' '}
            <span className="mono">sequentia-cli signmessagewithprivkey "&lt;staking WIF&gt;" "$(cat levo-challenge.txt)"</span>.
          </div>
        </div>
        <form onSubmit={submitManual}>
          <div className="field">
            <label htmlFor="sig">Signature</label>
            <input id="sig" className="mono" value={pasted} autoComplete="off" required
                   onChange={(e) => setPasted(e.target.value)}
                   placeholder="base64 signature" />
          </div>
          <div className="field">
            <label htmlFor="sigaddr">Address you signed with <span className="dim">(optional)</span></label>
            <input id="sigaddr" className="mono" value={address} autoComplete="off"
                   onChange={(e) => setAddress(e.target.value)}
                   placeholder="a legacy or tb1q… address of the signing key" />
            <div className="hint">
              A signature over slightly different text recovers to a key nobody
              holds. Naming the address turns that into an error instead of a
              phantom account.
            </div>
          </div>
          {error && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{error}</Notice>}
          <div className="btn-row">
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
      {error && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{error}</Notice>}
      <div className="btn-row">
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
          No Sequentia wallet was found in this browser. You can still sign the
          challenge with any wallet that signs messages and paste the result.
        </p>
      )}
    </div>
  )
}
