import { Link } from 'react-router-dom'
import { useStore } from '../lib/store'
import Beam from '../components/Beam'
import { compact } from '../lib/format'

export default function Home() {
  const { tiers, standing, signedIn } = useStore()
  const list = tiers ? tiers.tiers : null
  const stake = standing ? standing.stake_atoms : 0

  return (
    <>
      <header className="hero wrap">
        <p className="eyebrow">Launchpad · Sequentia</p>
        <h1>Your stake is the counterweight.</h1>
        <p className="hero-lede">
          Levo allocates by commitment. Stake Sequence and you can take a
          position in a sale; stake enough and you can run one. Everything
          in between is settled by a covenant that holds the project's tokens
          from the moment they are locked to the moment they reach a buyer.
        </p>
        <div className="hero-actions">
          <Link className="btn btn-primary" to="/projects">See open sales</Link>
          <Link className="btn btn-ghost" to="/how-it-works">How a sale settles</Link>
        </div>

        {list && (
          <div className="hero-beam">
            <div className="hero-beam-head">
              <p className="eyebrow" style={{ margin: 0 }}>
                {signedIn ? 'Where you sit' : 'What stake buys'}
              </p>
              <p className="small dim" style={{ margin: 0 }}>
                {signedIn
                  ? standing.next_tier
                    ? compact(standing.to_next_atoms) + ' SEQ more reaches ' + standing.next_tier.name
                    : 'Top tier. Listing is open to you.'
                  : 'Sign in to see your own position on the beam.'}
              </p>
            </div>
            <Beam tiers={list} stakeAtoms={stake} showMarker={signedIn} />
          </div>
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
              terms. Until that output exists and matches, the sale is a draft.
              The address is the terms, so a lock that verifies is proof rather
              than a claim.
            </p>
          </div>
          <div className="card">
            <h3>Payment and delivery are one transaction</h3>
            <p className="small dim" style={{ marginBottom: 0 }}>
              A buy spends the covenant and pays the treasury in the same spend.
              There is no moment where the project has been paid and you have
              not been delivered, and no moment where Levo holds either side.
            </p>
          </div>
          <div className="card">
            <h3>A live sale cannot be changed</h3>
            <p className="small dim" style={{ marginBottom: 0 }}>
              The price, the treasury, the token and the close date are compiled
              into the covenant and committed in its address. The project cannot
              reprice, redirect or withdraw a sale once it is funded. Neither
              can we.
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
                The first tier starts where the chain's own does. Below 40,000
                staked SEQ, consensus ignores a staker entirely, so Levo does
                too. Only staked SEQ counts, and only for keys you have proven
                you control.
              </p>
            </div>
            {list && (
              <div>
                {list.filter((t) => t.level > 0).map((t) => (
                  <div key={t.level} className="card" style={{ marginBottom: '.75rem' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', alignItems: 'baseline', flexWrap: 'wrap' }}>
                      <h3>{t.name}</h3>
                      <span className="num small dim">
                        {compact(t.min_stake_atoms)} SEQ staked
                      </span>
                    </div>
                    <p className="small dim" style={{ margin: '.5rem 0 0' }}>{t.blurb}</p>
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
                Sequence can stake.
              </p>
              <p className="small dim" style={{ marginBottom: 0 }}>
                Levo's tiers ride on that exception rather than on holdings. A
                large unstaked balance confers nothing here. The tier is a claim
                about committed stake, not about wealth.
              </p>
            </div>
          </div>
        </div>
      </section>
    </>
  )
}
