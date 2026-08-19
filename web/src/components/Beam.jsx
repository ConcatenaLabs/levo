import { SEQ, compact } from '../lib/format'

// Levo's one drawing, used at three sizes.
//
// It answers two questions with the same marks: which tier a stake reaches, and
// how much further the next one is. The scale is piecewise-linear -- each tier
// occupies an equal span -- because the thresholds are orders of magnitude
// apart and a true linear axis would collapse the lower two into the left edge.
// The fulcrum sits under the marker because that is the honest picture of the
// rule: your committed weight is what the allowance pivots on.

function position(stakeAtoms, tiers) {
  const stops = tiers.map((t) => t.min_stake_atoms)
  const n = stops.length - 1
  if (n < 1) return 0
  const s = Number(stakeAtoms || 0)
  if (s >= stops[n]) return 100
  for (let i = 0; i < n; i++) {
    const lo = stops[i], hi = stops[i + 1]
    if (s < hi) {
      const within = hi > lo ? (s - lo) / (hi - lo) : 0
      return ((i + within) / n) * 100
    }
  }
  return 100
}

export default function Beam({ tiers, stakeAtoms = 0, compactMode = false, showMarker = true }) {
  if (!tiers || tiers.length < 2) return null
  const n = tiers.length - 1
  const pos = position(stakeAtoms, tiers)

  return (
    <div className={'beam' + (compactMode ? ' compact' : '')} style={{ '--pos': pos + '%' }}>
      <div className="beam-track">
        <div className="beam-fill" />
        {tiers.map((t, i) => {
          const left = (i / n) * 100
          const reached = Number(stakeAtoms || 0) >= t.min_stake_atoms
          return (
            <div key={t.level}>
              <div
                className={'beam-tick' + (reached ? ' reached' : '')}
                style={{ left: left + '%' }}
              />
              <div
                className={'beam-label' + (reached ? ' reached' : '')}
                style={{
                  left: left + '%',
                  transform: i === 0 ? 'none' : i === n ? 'translateX(-100%)' : 'translateX(-50%)',
                }}
              >
                <b>{t.name}</b>
                <span>
                  {t.min_stake_atoms === 0
                    ? 'no stake'
                    : compact(t.min_stake_atoms, 8) + ' SEQ'}
                </span>
              </div>
            </div>
          )
        })}
        {showMarker && (
          <div className="beam-marker">
            <div className="beam-weight" />
            <div className="beam-fulcrum" />
            {!compactMode && (
              <div className="beam-you">{compact(stakeAtoms, 8)} staked</div>
            )}
          </div>
        )}
      </div>
      <div className="beam-scale" />
    </div>
  )
}
