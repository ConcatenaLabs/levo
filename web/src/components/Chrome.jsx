import { NavLink, Link } from 'react-router-dom'
import { useStore } from '../lib/store'
import { shortHex } from '../lib/format'

function Mark() {
  // A weight resting on a fulcrum: the same figure the beam draws, and the
  // same one the favicon carries.
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <path d="M4 21h24" stroke="var(--brass)" strokeWidth="2" strokeLinecap="round" />
      <path d="M16 21l-5.5 7h11z" fill="var(--verdigris)" />
      <rect x="11" y="7" width="10" height="8" rx="1" fill="none"
            stroke="var(--brass)" strokeWidth="2" />
    </svg>
  )
}

export function Nav() {
  const { signedIn, tier, account, loading, nodeDown, meError } = useStore()
  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <nav className="nav" aria-label="Main">
        <div className="wrap nav-in">
          <Link to="/" className="brand">
            <Mark />
            <span className="brand-word">Levo</span>
          </Link>
          <div className="nav-links">
            <NavLink to="/projects">Sales</NavLink>
            <NavLink to="/launch">Launch</NavLink>
            <NavLink to="/how-it-works">How it works</NavLink>
            <NavLink to="/account">
              {loading ? <span className="dim">…</span> : signedIn ? (
                <span className="mono" style={{ fontSize: '.82rem' }}>
                  {tier ? tier.name : ''} · {shortHex(account, 6, 4)}
                </span>
              ) : 'Sign in'}
            </NavLink>
          </div>
        </div>
      </nav>
      {nodeDown && (
        <div className="banner" role="status">
          <div className="wrap">
            Levo cannot reach its Sequentia node right now. Sale states may be stale, and
            purchases cannot be priced or built until it is back.
          </div>
        </div>
      )}
      {!nodeDown && meError && (
        <div className="banner" role="status">
          <div className="wrap">Your account could not be loaded: {meError}</div>
        </div>
      )}
    </>
  )
}

export function Footer() {
  const { config, links } = useStore()
  const named = Object.entries(links || {})
  return (
    <footer className="footer">
      <div className="wrap footer-in">
        <div style={{ flex: '1 1 320px' }}>
          <div className="brand" style={{ marginBottom: '.6rem' }}>
            <Mark /><span className="brand-word">Levo</span>
          </div>
          <p style={{ maxWidth: '46ch', margin: 0 }}>
            A launchpad on Sequentia. Project tokens sit in a covenant from lock
            to delivery, so a sale settles without anyone holding both sides.
          </p>
        </div>
        <div>
          <Link to="/how-it-works">How it works</Link>
          <Link to="/projects">Sales</Link>
          <Link to="/launch">Launch a project</Link>
        </div>
        {named.length > 0 && (
          <div>
            {named.map(([label, href]) => (
              <a key={label} href={href} target="_blank" rel="noopener noreferrer">{label}</a>
            ))}
          </div>
        )}
        <div className="small">
          {config.testnet
            ? 'Sequentia testnet. Tokens here carry no value.'
            : 'Sequentia.'}
        </div>
      </div>
    </footer>
  )
}
