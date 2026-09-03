import { Link } from 'react-router-dom'
import { useStore } from '../lib/store'
import Beam from '../components/Beam'
import { Notice, usePageTitle } from '../components/ui'
import { compact, tierSays } from '../lib/format'

export default function Home() {
  usePageTitle('')
  const { tiers, standing, signedIn, loading, config, payment, stake, links } = useStore()
  const list = tiers ? tiers.tiers : null
  const stakeAtoms = standing ? standing.stake_atoms : 0
  const first = list && list.length > 1 ? list[1] : null
  // The tier table is a second request and can fail on its own; config
  // carries the first threshold so the page can still say what it is.
  const firstAtoms = first ? first.min_stake_atoms : config.first_tier_atoms
  const floor = compact(config.staking_floor_atoms)
  const wallet = links.Wallet || links.wallet
  const faucet = links.Faucet || links.faucet
  const pools = links['Staking pools'] || links.pools

  return (
    <>
      <header className="hero wrap">
        <p className="eyebrow">Launchpad · Sequentia</p>
        <h1>Your stake is the counterweight.</h1>
        <p className="hero-lede">
          Levo allocates by commitment. Stake Sequence &mdash; the token
          Sequentia is secured with, ticker {stake.label} &mdash; and you can
          take a position in a sale; stake enough and you can run one.
          Everything in between is settled by a covenant that holds the
          project&rsquo;s tokens from the moment they are locked to the moment
          they reach a buyer. Sales here are priced in {payment.label}.
        </p>
        <div className="hero-actions">
          <Link className="btn btn-primary" to="/projects">See the sales</Link>
          <Link className="btn btn-ghost" to="/how-it-works">How it works</Link>
        </div>

        {list ? (
          <div className="hero-beam">
            <div className="hero-beam-head">
              <p className="eyebrow" style={{ margin: 0 }}>
                {signedIn ? 'Where you sit' : 'What stake buys'}
              </p>
              <p className="small dim" style={{ margin: 0 }}>
                {signedIn
                  ? standing.next_tier
                    ? compact(standing.to_next_atoms) + ' ' + stake.label + ' more reaches ' + standing.next_tier.name
                    : 'Top tier. Listing is open to you.'
                  : 'Sign in to see your own position on the beam.'}
              </p>
            </div>
            <Beam tiers={list} stakeAtoms={stakeAtoms} showMarker={signedIn}
                  paymentLabel={payment.label} stakeLabel={stake.label}
                        paymentDecimals={payment.decimals} stakeDecimals={stake.decimals} />
          </div>
        ) : !loading && (
          <Notice style={{ marginTop: '2rem' }}>The tier table could not be loaded right now.</Notice>
        )}
      </header>

      <section className="section wrap">
        <div className="section-head">
          <h2>What the chain guarantees</h2>
          <p>
            Levo is a place to find sales and a set of allocation rules. It is
            not a custodian, and the parts that protect your money do not
            depend on it behaving well.
          </p>
        </div>
        <div className="grid-3">
          <div className="card">
            <h3>Tokens are locked, not promised</h3>
            <p className="small dim" style={{ marginBottom: 0 }}>
              A project funds a covenant address derived from its published
              terms. Until that output exists and matches, the sale is not
              funded and nothing can be bought. The address is the terms, so a
              lock that verifies is proof rather than a claim.
            </p>
          </div>
          <div className="card">
            <h3>Payment and delivery are one transaction</h3>
            <p className="small dim" style={{ marginBottom: 0 }}>
              A buy spends the covenant and pays the project&rsquo;s treasury
              &mdash; an address the project fixed in the terms before the sale
              opened &mdash; in the same spend.
              There is no moment where the project has been paid and you have
              not been delivered, and no moment where Levo holds either side.
            </p>
          </div>
          <div className="card">
            <h3>The terms cannot change</h3>
            <p className="small dim" style={{ marginBottom: 0 }}>
              The price, the treasury, the token and the reclaim date are
              compiled into the covenant and committed in its address. Tokens
              leave it only at that price to that treasury, or back to the
              project after the close. Neither the project nor Levo can change
              the terms a buyer sees.
            </p>
          </div>
        </div>
      </section>

      <section className="section wrap">
        <div className="split">
          <div>
            <div className="section-head">
              <h2>Tiers</h2>
              <p>
                {config.first_tier_is_chain_floor
                  ? 'The first tier starts where the chain\'s own does: ' + floor + ' staked ' + stake.label +
                    ', the floor below which consensus ignores a staker entirely.'
                  : 'The first tier opens at ' + compact(firstAtoms) + ' staked ' + stake.label +
                    '. The chain\'s own blocksigner floor is ' + floor + '.'}
                {' '}Every proven key's stake adds to your total, and the total decides
                the tier. Only staked Sequence counts, and only for keys you have
                proven you control.
              </p>
            </div>
            {list && (
              <div>
                {list.filter((t) => t.level > 0).map((t) => (
                  <div key={t.level} className="card" style={{ marginBottom: '.75rem' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', alignItems: 'baseline', flexWrap: 'wrap' }}>
                      <h3>{t.name}</h3>
                      <span className="num small dim">
                        {compact(t.min_stake_atoms)} {stake.label} staked
                      </span>
                    </div>
                    <p className="small dim" style={{ margin: '.5rem 0 0' }}>
                      {tierSays(t, payment.label, payment.decimals)}
                    </p>
                    {t.blurb && <p className="small dim" style={{ margin: '.35rem 0 0' }}>{t.blurb}</p>}
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="sticky">
            <div className="card">
              <h3>Why staking, and only staking</h3>
              <p className="small dim">
                Sequentia has no privileged coin. Fees are payable in any
                accepted asset and every issued asset stands equal. Staking is
                the one exception the protocol itself makes, because only
                Sequence can stake. That every sale here is priced in{' '}
                {payment.label} is Levo&rsquo;s own policy and not the
                chain&rsquo;s: it is what lets any two sales be compared, and a
                covenant will hold terms in any asset at all.
              </p>
              <p className="small dim" style={{ marginBottom: 0 }}>
                Levo's tiers ride on that exception rather than on holdings. A
                large unstaked balance confers nothing here. The tier is a claim
                about committed stake, not about wealth.
              </p>
            </div>
            <div className="card" style={{ marginTop: '1rem' }}>
              <h3>Getting started</h3>
              <ol className="small dim" style={{ paddingLeft: '1.1rem', margin: 0 }}>
                <li>
                  Get a Sequentia wallet. The browser extension signs in here in one
                  click; any wallet that can sign a message works too, by pasting the
                  signature{wallet ? <>. <a href={wallet} target="_blank" rel="noopener noreferrer">Where to get one</a></> : ''}.
                </li>
                {config.testnet && faucet && (
                  <li>Get testnet {stake.label} and {payment.label} from <a href={faucet} target="_blank" rel="noopener noreferrer">the faucet</a>.</li>
                )}
                <li>
                  Stake {compact(firstAtoms, stake.decimals)} {stake.label}. From a
                  node that holds them:{' '}
                  <span className="mono">sequentia-cli registerstake &lt;staking pubkey&gt; &lt;amount&gt;</span>{' '}
                  bonds the coins to that key and locks them for the unbonding
                  delay; that key is the one you sign in with below.
                  {pools ? <> Delegating it to <a href={pools} target="_blank" rel="noopener noreferrer">a pool</a> afterwards
                    lends the block-signing rights and not the coins, so it still counts for you.</> : ''}
                </li>
                <li><Link to="/account">Sign in</Link> with the staking key, and your tier is on the first screen.</li>
              </ol>
            </div>
          </div>
        </div>
      </section>
    </>
  )
}
