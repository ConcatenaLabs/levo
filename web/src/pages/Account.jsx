import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { signStakerMessage, supportsStakerSigning, getStakerPublicKey, hasProvider,
         friendly } from '../lib/wallet'
import { amount, capitalise, compact, shortHex, timeLabel } from '../lib/format'
import { Copy, Hex, Notice, usePageTitle } from '../components/ui'
import SignIn from '../components/SignIn'
import Beam from '../components/Beam'
import { Status } from './Projects'

// Linking a staking key is the same primitive as signing in, aimed at a
// different key: sign a statement that names both the account and the key, and
// the stake behind that key starts counting.

function LinkKey({ onLinked }) {
  const [canOneClick, setCanOneClick] = useState(false)
  useEffect(() => { if (hasProvider()) supportsStakerSigning().then(setCanOneClick) }, [])
  const [pubkey, setPubkey] = useState('')
  const [challenge, setChallenge] = useState(null)
  const [sig, setSig] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function start(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const pk = pubkey.trim().toLowerCase()
      const ch = await api.stakeChallenge(pk)
      setChallenge(ch)
      if (hasProvider()) {
        // Only the wallet's own staking key can sign this usefully. The master
        // key would sign happily and be rejected, so it is not asked.
        try {
          const mine = await getStakerPublicKey()
          if (mine === pk) setSig((await signStakerMessage(ch.message)).signature)
        } catch { /* the wallet does not hold this key; the statement can be signed elsewhere */ }
      }
    } catch (e) { setError(capitalise(friendly(e))) } finally { setBusy(false) }
  }

  async function submit(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const st = await api.stakeLink(challenge.message, sig.trim(), pubkey.trim().toLowerCase())
      setChallenge(null); setSig(''); setPubkey('')
      onLinked(st)
    } catch (e) {
      setError(capitalise(e.message))
      try { setChallenge(await api.stakeChallenge(pubkey.trim().toLowerCase())) } catch {}
    } finally { setBusy(false) }
  }

  // Ask the wallet which key its stake is bonded to, then have it prove that
  // key. Two fields the user would have to find become one button.
  async function oneClick() {
    setError(null); setBusy(true)
    try {
      const pk = await getStakerPublicKey()
      const ch0 = await api.stakeChallenge(pk)
      const signed = await signStakerMessage(ch0.message)
      onLinked(await api.stakeLink(ch0.message, signed.signature,
                                   (signed.staker_pubkey || pk).toLowerCase()))
    } catch (e) {
      setError(capitalise(friendly(e)))
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
            The key your stake is bonded to: the <span className="mono">pubkey</span> you passed
            to <span className="mono">registerstake</span>. On a node, <span className="mono">sequentia-cli getstakerinfo true true</span> lists
            every controller, whether or not it delegates to a pool.
          </div>
        </div>
        {error && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{error}</Notice>}
        <div className="btn-row">
          <button className="btn btn-primary" disabled={busy || !pubkey.trim()}>
            {busy ? 'Working…' : 'Prove I control this key'}
          </button>
          {canOneClick && (
            <button type="button" className="btn btn-ghost" disabled={busy}
                    onClick={oneClick}>
              Use my wallet's staking key
            </button>
          )}
        </div>
      </form>
    )
  }

  const rows = challenge.message.split('\n').length + 1
  return (
    <form onSubmit={submit}>
      <div className="field">
        <label htmlFor="stmt">Sign this with the staking key <Copy value={challenge.message} label="Copy the statement" /></label>
        <textarea id="stmt" className="mono fit" readOnly rows={rows} value={challenge.message}
                  onFocus={(e) => e.target.select()} />
        <div className="hint">
          On a node: <span className="mono">sequentia-cli dumpprivkey &lt;the address you registered the stake from&gt;</span> gives
          the key's WIF. Save the statement above as{' '}
          <span className="mono">levo-link.txt</span>, then{' '}
          <span className="mono">sequentia-cli signmessagewithprivkey "&lt;WIF&gt;" "$(cat levo-link.txt)"</span>.
          Or <span className="mono">levo link --pubkey &lt;key&gt; --address &lt;a legacy address of that key&gt;</span>.
        </div>
      </div>
      <div className="field">
        <label htmlFor="ssig">Signature</label>
        <input id="ssig" className="mono" value={sig} autoComplete="off"
               onChange={(e) => setSig(e.target.value)} placeholder="base64 signature" />
      </div>
      {error && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{error}</Notice>}
      <div className="btn-row">
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

function MyProjects() {
  const [projects, setProjects] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => { api.myProjects().then((r) => setProjects(r.projects)).catch((e) => setError(e.message)) }, [])
  if (error) return <Notice kind="bad">{error}</Notice>
  if (!projects) return <p className="dim small">Loading…</p>
  if (!projects.length) return <p className="dim small">You have not listed a project. <Link to="/launch">Launch one.</Link></p>
  return (
    <div>
      {projects.map((p) => (
        <Link key={p.slug} to={'/p/' + p.slug} className="card" style={{ display: 'block', marginBottom: '.75rem', textDecoration: 'none' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap', alignItems: 'baseline' }}>
            <span><b>{p.name}</b> <span className="row-ticker">{p.ticker}</span></span>
            <Status sale={p.sale} />
          </div>
          {p.sale && (p.sale.status === 'draft' || p.sale.status === 'ghost') && (
            <p className="small dim" style={{ margin: '.5rem 0 0' }}>
              {p.sale.status === 'ghost' ? 'The lock was undone by a reorg. ' : ''}Lock the tokens on the sale's page to open it.
            </p>
          )}
          {p.sale && p.sale.strays && p.sale.strays.length > 0 && (
            <p className="small" style={{ margin: '.5rem 0 0', color: 'var(--alarm)' }}>Other assets are resting at the sale address.</p>
          )}
        </Link>
      ))}
    </div>
  )
}

function MyPositions() {
  const { payment, explorer } = useStore()
  const [positions, setPositions] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => { api.myPositions().then((r) => setPositions(r.positions)).catch((e) => setError(e.message)) }, [])
  if (error) return <Notice kind="bad">{error}</Notice>
  if (!positions) return <p className="dim small">Loading…</p>
  if (!positions.length) return <p className="dim small">No purchases through Levo yet. <Link to="/projects">See the sales.</Link></p>
  return (
    <div>
      {positions.map((p) => (
        <div key={p.slug} className="card" style={{ marginBottom: '.75rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap', alignItems: 'baseline' }}>
            <Link to={'/p/' + p.slug}><b>{p.name}</b> <span className="row-ticker">{p.ticker}</span></Link>
            <span className="num small">{amount(p.tokens_atoms, p.decimals)} {p.ticker}</span>
          </div>
          <div className="kv" style={{ marginTop: '.5rem' }}><span>Committed</span><b>{amount(p.committed_atoms, payment.decimals)} {payment.label}</b></div>
          <div className="kv"><span>Still open in your cap</span><b>{amount(p.allowance_remaining_atoms, payment.decimals)} {payment.label}</b></div>
          {p.purchases.map((e) => (
            <div key={e.txid + e.at} className="small dim" style={{ marginTop: '.4rem' }}>
              {timeLabel(e.at)}: {amount(e.token_atoms, p.decimals)} {p.ticker} for {amount(e.payment_atoms, payment.decimals)} {payment.label}
              {e.txid && <> · <Hex value={e.txid} href={explorer('tx', e.txid)} short={12} copy={false} /></>}
              {e.verified === true ? ' · treasury payment checked' : e.verified === false ? '' : ' · not checked on chain'}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

export default function Account() {
  usePageTitle('Account')
  const { signedIn, standing, tiers, account, signOut, setStanding, loading, config, payment, stake } = useStore()
  const [unlinkError, setUnlinkError] = useState(null)
  const [busyKey, setBusyKey] = useState(null)

  if (loading) return <div className="wrap section"><h1 className="h2">Account</h1><p className="dim">Loading your account…</p></div>

  if (!signedIn) {
    return (
      <div className="wrap section" style={{ maxWidth: 640 }}>
        <div className="section-head">
          <h1 className="h2">Sign in</h1>
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

  async function unlink(k) {
    setUnlinkError(null); setBusyKey(k)
    try { setStanding(await api.stakeUnlink(k)) } catch (e) { setUnlinkError(capitalise(e.message)) } finally { setBusyKey(null) }
  }

  const st = standing
  const floor = compact(config.staking_floor_atoms)
  return (
    <div className="wrap section">
      <div className="section-head">
        <p className="eyebrow">Account</p>
        <h1 className="h2">{st.tier.name}</h1>
        <p className="mono small" style={{ overflowWrap: 'anywhere' }}>{account} <Copy value={account} label="Copy the account key" /></p>
      </div>

      {tiers && (
        <div style={{ marginBottom: '2.5rem' }}>
          <Beam tiers={tiers.tiers} stakeAtoms={st.stake_atoms} paymentLabel={payment.label} stakeLabel={stake.label} />
        </div>
      )}

      <div className="grid-3" style={{ marginBottom: '2.5rem' }}>
        <div className="stat">
          <b title={amount(st.stake_atoms, stake.decimals) + ' ' + stake.label}>
            {compact(st.stake_atoms)}
          </b>
          <span>{stake.label} staked</span>
          <span className="num" style={{ fontSize: '.78rem' }}>
            {amount(st.stake_atoms, stake.decimals)} exactly
          </span>
        </div>
        <div className="stat">
          <b>{amount(st.tier.cap_atoms, payment.decimals)}</b><span>{payment.label} cap per sale</span>
        </div>
        <div className="stat">
          <b>{st.next_tier ? compact(st.to_next_atoms) : '—'}</b>
          <span>{st.next_tier ? stake.label + ' to ' + st.next_tier.name : 'top tier'}</span>
          {st.next_tier && (
            <span className="num" style={{ fontSize: '.78rem' }}>
              {amount(st.to_next_atoms, stake.decimals)} exactly
            </span>
          )}
        </div>
      </div>

      <div className="split">
        <div>
          <h2 style={{ marginBottom: '.75rem' }}>Staking keys</h2>
          <p className="small dim">
            Stake counts only for keys you have proven you control. One key
            counts for one account: proving it here, or signing in with it, moves
            it off any account that claimed it before.
          </p>
          {st.staking_available === false && (
            <Notice style={{ marginBottom: '1.25rem' }}>{st.delegation_note}</Notice>
          )}
          {st.staking_available !== false && st.counts_delegated_stake === false && (
            <Notice kind="bad" style={{ marginBottom: '1.25rem' }}>{st.delegation_note}</Notice>
          )}
          {st.keys.length === 0 && (
            <Notice style={{ marginBottom: '1.25rem' }}>
              No staking keys are proven yet, so your stake reads as zero.
            </Notice>
          )}
          {unlinkError && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{unlinkError}</Notice>}
          {st.keys.map((k) => (
            <div className="card" key={k.staker_pubkey} style={{ marginBottom: '.75rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap', alignItems: 'center' }}>
                <span className="mono small" style={{ overflowWrap: 'anywhere' }}>
                  {shortHex(k.staker_pubkey, 16, 10)}
                </span>
                <span className="num small">{compact(k.weight_atoms)} {stake.label}</span>
              </div>
              {k.delegated && (
                <p className="small dim" style={{ margin: '.5rem 0 0' }}>
                  Delegated to a pool ({k.delegated_to ? k.delegated_to.slice(0, 16) + '…' : 'a signer'}).
                  It still counts here: delegation lends block-signing rights,
                  never the coins.
                </p>
              )}
              {!k.eligible_blocksigner && (
                <p className="small dim" style={{ margin: '.5rem 0 0' }}>
                  Below the chain's {floor} {stake.label} blocksigner floor. It still adds
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
                        disabled={busyKey === k.staker_pubkey}
                        onClick={() => unlink(k.staker_pubkey)}>
                  {busyKey === k.staker_pubkey ? 'Unlinking…' : 'Unlink'}
                </button>
              )}
            </div>
          ))}

          <h2 style={{ margin: '2.5rem 0 .75rem' }}>Your positions</h2>
          <MyPositions />

          <h2 style={{ margin: '2.5rem 0 .75rem' }}>Your projects</h2>
          <MyProjects />
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
