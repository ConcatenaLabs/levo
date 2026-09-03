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

DELEGATED STAKE COUNTS, and counts for the person who owns it. Sequentia lets a
staker lend its block-signing rights to a pool without moving the coins: the
pool produces blocks with that weight, and only the controller can ever spend
them. The chain's default view is keyed by SIGNER, because that is what block
production needs, and under it a delegator appears to have no stake at all
while their pool appears to have everyone's.

Levo asks a different question -- whose money is this? -- so it reads weight
keyed by CONTROLLER. A delegator keeps their tier, and a pool operator is
credited with their own stake rather than with their depositors'. Anything else
would either shut out the people who delegate or hand a tier to whoever
happened to be trusted with the signing.
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


# The defaults below are written in whole units of an asset with eight places,
# which is what Elements issues by default. A `cap` in LEVOD_TIERS is in whole
# units too, and is converted at the deployment's own precision -- reading it
# at eight places on a two-place asset would multiply every cap by a million
# and stop it binding at all.
USDX_ATOMS = 100_000_000

# The default thresholds are shares of the 400,000,000 SEQ supply, which is
# where the protocol's own floor comes from: 40,000 SEQ is 0.01% (whitepaper
# section 3.3). Expressing them that way rather than as round numbers means they
# keep their meaning as the network grows.
#
#   Contributor  0.01%   the floor below which consensus ignores a staker
#   Backer       0.05%
#   Founder      0.25%   and the only tier that may list
# A blurb says what a tier MEANS. It quotes no figure and no ticker: the cap
# and the threshold are data, shown beside it in whatever units the deployment
# actually runs -- tSEQ on a testnet, SEQ on mainnet -- and a blurb repeating
# them from memory is how a card ends up saying "50,000 SEQ" next to
# "50,000 tSEQ".
DEFAULT_TIERS = [
    Tier(0, "Visitor", 0, 0, False,
         "Read every sale and check every address it publishes. Staking opens "
         "the first allocation tier."),
    Tier(1, "Contributor", POS_MIN_STAKE_ATOMS, 1_000 * USDX_ATOMS, False,
         "0.01% of the supply staked, which is the chain's own blocksigner "
         "floor: below it, consensus ignores a staker's weight entirely."),
    Tier(2, "Backer", 5 * POS_MIN_STAKE_ATOMS, 10_000 * USDX_ATOMS, False,
         "0.05% of the supply staked."),
    Tier(3, "Founder", 25 * POS_MIN_STAKE_ATOMS, 100_000 * USDX_ATOMS, True,
         "0.25% of the supply staked, and the only tier that may list a project."),
]


def atoms_per_unit(decimals=8):
    """One whole unit of the payment asset, in atoms."""
    return 10 ** int(decimals)


def _cap_atoms(cap, unit, name):
    """A cap in whole units, as atoms, or a refusal.

    A cap that is not a whole number of atoms at this deployment's precision is
    a typo, and rounding it silently is how a cap stops meaning what its table
    says.
    """
    atoms = float(cap) * unit
    if abs(atoms - round(atoms)) > 1e-6:
        raise ValueError(
            "the %s tier's cap of %s is not a whole number of atoms at this "
            "deployment's precision" % (name or "unnamed", cap))
    return int(round(atoms))


def tiers_from_env(env=None, payment_decimals=8):
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
    unit = atoms_per_unit(payment_decimals)
    spec = json.loads(raw)
    tiers = []
    for level, t in enumerate(sorted(spec, key=lambda x: float(x.get("min_stake", 0)))):
        tiers.append(Tier(
            level, t["name"],
            int(round(float(t.get("min_stake", 0)) * SEQ_ATOMS)),
            _cap_atoms(t.get("cap", 0), unit, t.get("name")),
            bool(t.get("may_list", False)),
            t.get("blurb", "")))
    if not tiers:
        raise ValueError("LEVOD_TIERS is empty")
    if not any(t.may_list for t in tiers):
        raise ValueError("LEVOD_TIERS has no tier that may list a project")
    if tiers[0].min_stake_atoms != 0:
        raise ValueError("the lowest tier must start at 0 stake, so that every "
                         "visitor lands somewhere")
    for t in tiers:
        if t.cap_atoms < 0 or t.min_stake_atoms < 0:
            raise ValueError("LEVOD_TIERS tier %r has a negative threshold or cap" % t.name)
    for a, b in zip(tiers, tiers[1:]):
        if b.min_stake_atoms == a.min_stake_atoms:
            raise ValueError("LEVOD_TIERS has two tiers at %s SEQ; each threshold "
                             "must be distinct" % (b.min_stake_atoms / SEQ_ATOMS))
        if b.cap_atoms < a.cap_atoms:
            raise ValueError("LEVOD_TIERS tier %r has a smaller cap than the tier "
                             "below it; more stake must never buy less" % b.name)
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

    def listing_tier(self):
        """The lowest tier that may list a project."""
        for t in self.tiers:
            if t.may_list:
                return t
        return self.tiers[-1]

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
        self.dirty = False         # a link moved outside an explicit link call

    def owner_of(self, staker_pubkey):
        return self._owner.get(str(staker_pubkey).lower())

    PURPOSE = "Link this staking key to a Levo account."

    @staticmethod
    def binding_lines(account_pubkey, staker_pubkey):
        """The lines that make a link statement bind: both parties, by name."""
        return ["Account: %s" % account_pubkey, "Staking key: %s" % staker_pubkey]

    @staticmethod
    def binding_statement(account_pubkey, staker_pubkey, nonce):
        """The statement shape a challenge issues for a link, for callers that
        compose one without a Challenges instance (tests, documentation)."""
        return "\n".join(
            ["Levo", "", StakeLinks.PURPOSE,
             "This signature proves you control this wallet. It authorises no "
             "payment and moves no funds.", ""]
            + StakeLinks.binding_lines(account_pubkey, staker_pubkey)
            + ["Nonce: %s" % nonce])

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

    def __init__(self, rpc, links, policy=None, floor=None):
        self.rpc = rpc
        self.links = links
        self.policy = policy or TierPolicy()
        # What the CHAIN requires of a signer, when something can ask it.
        # POS_MIN_STAKE_ATOMS is this network's value and not every network's.
        self._floor = floor

    @property
    def floor(self):
        value = self._floor() if callable(self._floor) else self._floor
        return POS_MIN_STAKE_ATOMS if value is None else value

    def standing(self, account_pubkey):
        """The account's staked total, its tier, and the evidence behind both.

        If the key you signed in with is ITSELF a registered staker, it counts
        without a separate linking step. Linking exists to prove control of a
        key you did not sign in with; asking for a second signature under the
        same key would prove nothing the login has not already proved.
        """
        weights, by_controller = self.rpc.controller_weights()
        detail = []
        total = 0
        keys = list(self.links.keys_for(account_pubkey))
        if account_pubkey in weights and account_pubkey not in keys:
            # Signing in with a staking key proves control of it as surely as
            # a link statement does. If the key was linked to another account
            # before, the newest proof wins and the key moves, so one stake
            # never counts for two accounts at once.
            if self.links.owner_of(account_pubkey) not in (None, account_pubkey):
                self.links.link(account_pubkey, account_pubkey)
                self.links.dirty = True
            keys.insert(0, account_pubkey)
        for k in keys:
            entry = weights.get(k) or {}
            w = int(entry.get("weight_atoms", 0))
            detail.append({"staker_pubkey": k, "weight_atoms": w,
                           "weight": w / SEQ_ATOMS,
                           "counted": w > 0,
                           "is_login_key": k == account_pubkey,
                           "delegated": bool(entry.get("delegated")),
                           "delegated_to": entry.get("signer"),
                           "eligible_blocksigner": w >= self.floor})
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
            "staking_available": by_controller is not None,
            "counts_delegated_stake": by_controller,
            "delegation_note": (
                "Stake delegated to a pool counts for you: delegation lends "
                "block-signing rights, never the coins."
                if by_controller else
                "This chain has no proof of stake, so there is no stake to "
                "count and every account is a visitor here."
                if by_controller is None else
                "This node reports stake by signer, so a stake you have "
                "delegated to a pool will not be counted here. It is not lost; "
                "the node needs a build that can report weight by controller."),
        }
