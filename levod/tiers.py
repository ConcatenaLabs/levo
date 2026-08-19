"""Levo tiers: what your stake entitles you to.

Levo allocates by skin in the game. Your tier is decided by how much Sequence
(SEQ) you have staked with keys you control, and your tier decides how much you
may put into any one sale -- and, at the top, whether you may list a project at
all.

Two things about that are deliberate.

The first tier begins at the protocol's OWN eligibility floor: 40,000 SEQ, the
minimum weight a key must hold before consensus counts it as a blocksigner at
all (whitepaper section 3.3, `-posminstake`). Stake below that floor buys no
influence over the chain, so it buys no allocation here either. Levo does not
invent an economic threshold; it borrows the one the chain already enforces.

The second is that this is the ONLY place SEQ is privileged. Sequentia has no
privileged coin: fees are payable in any accepted asset and every issued asset
stands equal. Staking is the single exception the protocol itself makes -- only
SEQ can stake -- and Levo's tiers ride on staking, not on holdings. A large SEQ
balance that is not staked confers nothing; a project's own token confers
nothing. The tier is a claim about committed stake, not about wealth.

Stake is counted only for staker keys the account has PROVEN it controls, by
signing a binding statement with each key. An account cannot claim a stranger's
stake by naming their public key.
"""

import json
import os

SEQ_ATOMS = 100_000_000          # SEQ has 8 decimal places
POS_MIN_STAKE_ATOMS = 4_000_000_000_000   # 40,000 SEQ: the chain's own floor


class Tier:
    def __init__(self, level, name, min_stake_atoms, cap_atoms, may_list, blurb):
        self.level = level
        self.name = name
        self.min_stake_atoms = min_stake_atoms
        self.cap_atoms = cap_atoms          # per-sale investment cap, payment-asset atoms
        self.may_list = may_list
        self.blurb = blurb

    def to_json(self):
        return {
            "level": self.level,
            "name": self.name,
            "min_stake_atoms": self.min_stake_atoms,
            "min_stake": self.min_stake_atoms / SEQ_ATOMS,
            "cap_atoms": self.cap_atoms,
            "may_list": self.may_list,
            "blurb": self.blurb,
        }


# Cap figures are payment-asset atoms (USDX, 8 dp), per sale.
USDX_ATOMS = 100_000_000

# The default thresholds are shares of the 400,000,000 SEQ supply, which is
# where the protocol's own floor comes from: 40,000 SEQ is 0.01% (whitepaper
# section 3.3). Expressing them that way rather than as round numbers means they
# keep their meaning as the network grows.
#
#   Contributor  0.01%   the floor below which consensus ignores a staker
#   Backer       0.05%
#   Founder      0.25%   and the only tier that may list
DEFAULT_TIERS = [
    Tier(0, "Visitor", 0, 0, False,
         "Browse every sale. Staking 40,000 SEQ opens the first allocation tier."),
    Tier(1, "Contributor", POS_MIN_STAKE_ATOMS, 1_000 * USDX_ATOMS, False,
         "0.01% of supply staked, the chain's own blocksigner floor. Up to "
         "1,000 USDX into any one sale."),
    Tier(2, "Backer", 5 * POS_MIN_STAKE_ATOMS, 10_000 * USDX_ATOMS, False,
         "0.05% of supply staked. Up to 10,000 USDX into any one sale."),
    Tier(3, "Founder", 25 * POS_MIN_STAKE_ATOMS, 100_000 * USDX_ATOMS, True,
         "0.25% of supply staked. Up to 100,000 USDX into any one sale, and the "
         "only tier that may list a project."),
]


def tiers_from_env(env=None):
    """Thresholds an operator can set without editing code.

    LEVOD_TIERS is a JSON list of objects with `name`, `min_stake` (in whole
    SEQ), `cap` (in whole units of the payment asset), `may_list` and an
    optional `blurb`. A deployment whose stakers all sit near the protocol floor
    needs lower thresholds than mainnet defaults, and changing them is an
    operator's decision rather than a code change.

    The tier table the interface shows comes from here, so whatever is
    configured is what users are told.
    """
    env = env if env is not None else os.environ
    raw = env.get("LEVOD_TIERS")
    if not raw:
        return None
    spec = json.loads(raw)
    tiers = []
    for level, t in enumerate(sorted(spec, key=lambda x: float(x.get("min_stake", 0)))):
        tiers.append(Tier(
            level, t["name"],
            int(round(float(t.get("min_stake", 0)) * SEQ_ATOMS)),
            int(round(float(t.get("cap", 0)) * USDX_ATOMS)),
            bool(t.get("may_list", False)),
            t.get("blurb", "")))
    if not tiers:
        raise ValueError("LEVOD_TIERS is empty")
    if not any(t.may_list for t in tiers):
        raise ValueError("LEVOD_TIERS has no tier that may list a project")
    if tiers[0].min_stake_atoms != 0:
        raise ValueError("the lowest tier must start at 0 stake, so that every "
                         "visitor lands somewhere")
    return tiers


class TierPolicy:
    def __init__(self, tiers=None):
        self.tiers = sorted(tiers or DEFAULT_TIERS, key=lambda t: t.min_stake_atoms)

    def for_stake(self, stake_atoms):
        """The highest tier whose floor this stake reaches."""
        best = self.tiers[0]
        for t in self.tiers:
            if stake_atoms >= t.min_stake_atoms:
                best = t
        return best

    def next_tier(self, stake_atoms):
        """The tier above the current one, or None at the top."""
        current = self.for_stake(stake_atoms)
        for t in self.tiers:
            if t.level == current.level + 1:
                return t
        return None

    def to_json(self):
        return [t.to_json() for t in self.tiers]


class StakeLinks:
    """Which staker keys an account has proven it controls.

    A link is created by signing a statement that names BOTH the account and the
    staker key, so a signature collected for one purpose cannot be replayed to
    attach the same stake to a second account.
    """

    def __init__(self):
        self._by_account = {}      # account pubkey -> set of staker pubkeys
        self._owner = {}           # staker pubkey -> account pubkey

    @staticmethod
    def binding_statement(account_pubkey, staker_pubkey, nonce):
        return (
            "Levo\n\n"
            "Link this staking key to a Levo account.\n"
            "This signature proves you control the stake. It authorises no "
            "payment and moves no funds.\n\n"
            "Account: %s\n"
            "Staking key: %s\n"
            "Nonce: %s\n"
        ) % (account_pubkey, staker_pubkey, nonce)

    def link(self, account_pubkey, staker_pubkey):
        """Attach a proven staker key. One key, one account.

        Re-linking to a different account MOVES the key rather than duplicating
        it, so the same stake can never count towards two accounts' tiers at
        once. Whoever proves control most recently holds it -- which is the
        honest answer, since they demonstrably do.
        """
        staker_pubkey = staker_pubkey.lower()
        prior = self._owner.get(staker_pubkey)
        if prior and prior != account_pubkey:
            self._by_account[prior].discard(staker_pubkey)
        self._owner[staker_pubkey] = account_pubkey
        self._by_account.setdefault(account_pubkey, set()).add(staker_pubkey)

    def unlink(self, account_pubkey, staker_pubkey):
        staker_pubkey = staker_pubkey.lower()
        self._by_account.get(account_pubkey, set()).discard(staker_pubkey)
        if self._owner.get(staker_pubkey) == account_pubkey:
            del self._owner[staker_pubkey]

    def keys_for(self, account_pubkey):
        return sorted(self._by_account.get(account_pubkey, set()))

    def to_json(self):
        return {a: sorted(k) for a, k in self._by_account.items() if k}

    def load(self, d):
        for account, keys in (d or {}).items():
            for k in keys:
                self.link(account, k)


class StakeReader:
    """Turns 'who am I' into 'what am I allowed to do', against the live chain."""

    def __init__(self, rpc, links, policy=None):
        self.rpc = rpc
        self.links = links
        self.policy = policy or TierPolicy()

    def standing(self, account_pubkey):
        """The account's staked total, its tier, and the evidence behind both.

        If the key you signed in with is ITSELF a registered staker, it counts
        without a separate linking step. Linking exists to prove control of a
        key you did not sign in with; asking for a second signature under the
        same key would prove nothing the login has not already proved.
        """
        weights = self.rpc.staker_weights()
        detail = []
        total = 0
        keys = list(self.links.keys_for(account_pubkey))
        if account_pubkey in weights and account_pubkey not in keys:
            keys.insert(0, account_pubkey)
        for k in keys:
            w = weights.get(k, 0)
            detail.append({"staker_pubkey": k, "weight_atoms": w,
                           "weight": w / SEQ_ATOMS,
                           "counted": w > 0,
                           "is_login_key": k == account_pubkey,
                           "eligible_blocksigner": w >= POS_MIN_STAKE_ATOMS})
            total += w
        tier = self.policy.for_stake(total)
        nxt = self.policy.next_tier(total)
        return {
            "account": account_pubkey,
            "stake_atoms": total,
            "stake": total / SEQ_ATOMS,
            "tier": tier.to_json(),
            "next_tier": nxt.to_json() if nxt else None,
            "to_next_atoms": (nxt.min_stake_atoms - total) if nxt else 0,
            "keys": detail,
        }
