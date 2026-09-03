import { big, compact, positive } from '../lib/format'
import { xFor, yFor } from '../lib/beam'

// Levo's one drawing: the rule itself, plotted.
//
// Stake runs left to right, the cap steps up with it, and your own stake sits
// on the step it has reached. It is a step function because that is genuinely
// what the rule is -- the cap does not rise smoothly with stake, it jumps at
// thresholds -- and drawing it any other way would flatter the design at the
// cost of describing the thing wrongly.
//
// The geometry lives in lib/beam.js so it can be tested without a browser.

export default function Beam({ tiers, stakeAtoms = 0, compactMode = false, showMarker = true,
                               paymentLabel = 'USDX', stakeLabel = 'SEQ',
                               // A cap is an amount of the PAYMENT asset and a
                               // stake is an amount of the stake asset, and the
                               // two do not divide the same way.
                               paymentDecimals = 8, stakeDecimals = 8 }) {
  if (!tiers || tiers.length < 2) return null

  const W = 1000
  const H = compactMode ? 104 : 168
  const padB = compactMode ? 14 : 18
  const padT = compactMode ? 20 : 30
  // geometry: a log scale needs doubles, and a pixel does not care about
  // the last atom. Every decision drawn on top of these is made exactly.
  const stops = tiers.map((t) => Number(t.min_stake_atoms))  // geometry
  const caps = tiers.map((t) => Number(t.cap_atoms))  // geometry
  const maxCap = Math.max(...caps, 1)

  const seg = W / tiers.length
  const steps = tiers.map((t, i) => ({
    i, x0: i * seg, x1: (i + 1) * seg, y: yFor(caps[i], maxCap, padT, H - padB), tier: t,
  }))

  const stake = Number(stakeAtoms || 0)
  // Keep the marker inside the drawing: a zero stake sits at the very edge,
  // and half a dot is not a position.
  const mx = Math.min(W - 6, Math.max(6, xFor(stake, stops) * W))
  // Which tier the reader has reached is a claim, not a pixel, so it is decided
  // on the exact atom counts rather than on the floats the drawing uses. Every
  // label below reads its state from this one index for the same reason.
  const reachedIndex = tiers.reduce(
    (acc, t, i) => (big(stakeAtoms) >= big(t.min_stake_atoms) ? i : acc), 0)
  const my = steps[reachedIndex].y

  const path = steps.map((s, i) =>
    `${i === 0 ? 'M' : 'L'} ${s.x0} ${s.y} L ${s.x1} ${s.y}`).join(' ')

  // What a reader who cannot see the drawing needs from it: the rule, and
  // where they stand on it. A tier with no cap cannot buy, which is a
  // different fact from a cap of zero.
  const label = 'Per-sale cap by amount staked. ' + tiers.map((t) => {
    const from = positive(t.min_stake_atoms)
      ? 'from ' + compact(t.min_stake_atoms, stakeDecimals) + ' ' + stakeLabel + ' staked'
      : 'below the first threshold'
    return t.name + ', ' + from + ', ' +
      (positive(t.cap_atoms)
        ? 'up to ' + compact(t.cap_atoms, paymentDecimals) + ' ' + paymentLabel + ' per sale'
        : 'cannot buy') +
      (t.may_list ? ', and may list a project' : '')
  }).join('. ') + '.' + (showMarker && stake > 0
    ? ' You have ' + compact(stake, stakeDecimals) + ' ' + stakeLabel + ' staked, which is ' +
      steps[reachedIndex].tier.name + '.'
    : '')

  return (
    <div className={'beam' + (compactMode ? ' compact' : '')}>
      <div className="beam-head">
        <span>Stake, {stakeLabel} →</span>
        <span>cap per sale, {paymentLabel}</span>
      </div>
      <div className="beam-plot">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           role="img" aria-label={label}>
        {/* the ground the steps stand on */}
        <line x1="0" y1={H - padB} x2={W} y2={H - padB}
              stroke="var(--line)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        {/* filled area up to the reached step, so progress reads at a glance */}
        {showMarker && steps.slice(0, reachedIndex + 1).map((s) => (
          <rect key={'f' + s.i} x={s.x0} y={s.y}
                width={Math.max(0, (s.i === reachedIndex ? mx : s.x1) - s.x0)}
                height={Math.max(0, H - padB - s.y)}
                fill="var(--brass)" opacity="0.14" />
        ))}
        {/* the rule itself */}
        <path d={path} fill="none" stroke="var(--brass)" strokeWidth="2"
              vectorEffect="non-scaling-stroke" strokeLinejoin="miter" />
        {/* risers at each threshold */}
        {steps.slice(1).map((s) => (
          <line key={'r' + s.i} x1={s.x0} y1={steps[s.i - 1].y} x2={s.x0} y2={s.y}
                stroke="var(--brass)" strokeWidth="2"
                vectorEffect="non-scaling-stroke" />
        ))}
        {/* threshold ticks down to the labels */}
        {steps.slice(1).map((s) => (
          <line key={'t' + s.i} x1={s.x0} y1={H - padB} x2={s.x0} y2={H - padB + 6}
                stroke="var(--line)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        ))}
        {showMarker && (
          <g>
            <line x1={mx} y1={my} x2={mx} y2={H - padB}
                  stroke="var(--verdigris)" strokeWidth="1.5"
                  vectorEffect="non-scaling-stroke" strokeDasharray="3 3" />
            <circle cx={mx} cy={my} r="5" fill="var(--verdigris)"
                    stroke="var(--ground)" strokeWidth="2"
                    vectorEffect="non-scaling-stroke" />
          </g>
        )}
      </svg>
      {/* The step values live in HTML, not in the SVG: the geometry is scaled
          non-uniformly so the steps fill the width, and text inside it would be
          squashed with them. */}
      <div className="beam-caps" aria-hidden="true">
        {steps.map((s) => (
          big(s.tier.cap_atoms) > 0n ? (
            <span key={'cap' + s.i} className="beam-cap"
                  style={{ left: (s.x0 / W) * 100 + '%',
                           top: ((s.y / H) * 100) + '%' }}>
              {compact(s.tier.cap_atoms, paymentDecimals)}
            </span>
          ) : null
        ))}
      </div>
      </div>
      <div className="beam-labels" aria-hidden="true">
        {tiers.map((t, i) => (
          <div key={t.level}
               className={'beam-label' + (i <= reachedIndex ? ' reached' : '')}
               style={{ left: (i / tiers.length) * 100 + '%' }}>
            <b>{t.name}</b>
            <span>{!positive(t.min_stake_atoms) ? 'no stake'
                                                   : compact(t.min_stake_atoms, stakeDecimals) + ' ' + stakeLabel}</span>
          </div>
        ))}
      </div>
      {/* The same tiers as a list. Plotted names cannot be read on a narrow
          screen (four of them in 320px overprint each other) and the figures
          are what the panel is for. */}
      <ul className="beam-list" aria-hidden="true">
        {tiers.map((t, i) => (
          <li key={t.level} className={i <= reachedIndex ? 'reached' : ''}>
            <b>{t.name}</b>
            <span>
              {!positive(t.min_stake_atoms)
                ? 'no stake'
                : compact(t.min_stake_atoms, stakeDecimals) + ' ' + stakeLabel}
              {positive(t.cap_atoms)
                ? ' · up to ' + compact(t.cap_atoms, paymentDecimals) + ' ' + paymentLabel
                : ''}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
