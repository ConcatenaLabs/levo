"""Tier checks: thresholds, proof of control, and the sale rules around them."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import sale as S  # noqa: E402
import tiers as T  # noqa: E402

SEQ = T.SEQ_ATOMS
FLOOR = T.POS_MIN_STAKE_ATOMS


def test_tier_boundaries(t):
    p = T.TierPolicy()
    t.eq(p.for_stake(0).name, "Visitor", "no stake is a visitor")
    t.eq(p.for_stake(FLOOR - 1).name, "Visitor", "one atom below the floor is still a visitor")
    t.eq(p.for_stake(FLOOR).name, "Contributor", "the chain's floor opens tier 1")
    t.eq(p.for_stake(5 * FLOOR).name, "Backer", "five times the floor is tier 2")
    t.eq(p.for_stake(25 * FLOOR).name, "Founder", "25 times the floor is tier 3")
    t.eq(p.for_stake(10**18).name, "Founder", "there is nothing above the top tier")


def test_only_the_top_tier_may_list(t):
    p = T.TierPolicy()
    t.eq([x.name for x in p.tiers if x.may_list], ["Founder"],
         "exactly one tier may list a project")
    t.eq(p.for_stake(0).cap_atoms, 0, "a visitor has no allocation")


def test_first_tier_matches_the_chains_own_floor(t):
    """Tier 1 is not an invented number: it is the weight below which consensus
    itself ignores a staker."""
    p = T.TierPolicy()
    tier1 = [x for x in p.tiers if x.level == 1][0]
    t.eq(tier1.min_stake_atoms, FLOOR, "tier 1 begins at -posminstake")
    t.eq(FLOOR // SEQ, 40_000, "the floor is 40,000 SEQ")


def test_a_staking_key_counts_for_one_account_only(t):
    """Otherwise two accounts could each claim the same stake and both buy at a
    tier neither has paid for."""
    links = T.StakeLinks()
    key = "02" + "aa" * 32
    links.link("account-a", key)
    t.eq(links.keys_for("account-a"), [key], "linked to the first account")
    links.link("account-b", key)
    t.eq(links.keys_for("account-a"), [], "moves away from the first account")
    t.eq(links.keys_for("account-b"), [key], "and onto the second")


def test_binding_statement_names_both_parties(t):
    """A signature proving control of a key must not be replayable onto a
    different account, so the statement names the account too."""
    s1 = T.StakeLinks.binding_statement("acct-1", "02" + "bb" * 32, "nonce")
    s2 = T.StakeLinks.binding_statement("acct-2", "02" + "bb" * 32, "nonce")
    t.ok(s1 != s2, "the statement differs per account")
    t.ok("acct-1" in s1 and "02" + "bb" * 32 in s1, "it names both parties")


class _FakeRPC:
    def __init__(self, weights):
        self._w = weights

    def staker_weights(self):
        return dict(self._w)


def test_standing_sums_only_proven_keys(t):
    k1, k2, stranger = "02" + "11" * 32, "02" + "22" * 32, "02" + "33" * 32
    rpc = _FakeRPC({k1: 3 * FLOOR, k2: 2 * FLOOR, stranger: 100 * FLOOR})
    links = T.StakeLinks()
    links.link("me", k1)
    links.link("me", k2)
    reader = T.StakeReader(rpc, links)
    st = reader.standing("me")
    t.eq(st["stake_atoms"], 5 * FLOOR, "sums the keys I proved")
    t.eq(st["tier"]["name"], "Backer", "and tiers on that total")
    t.ok(all(d["staker_pubkey"] != stranger for d in st["keys"]),
         "a stranger's stake is not counted")


def test_unstaked_keys_contribute_nothing(t):
    key = "02" + "44" * 32
    links = T.StakeLinks()
    links.link("me", key)
    reader = T.StakeReader(_FakeRPC({}), links)
    st = reader.standing("me")
    t.eq(st["stake_atoms"], 0, "a key with no stake on chain adds nothing")
    t.eq(st["tier"]["name"], "Visitor", "so the account stays a visitor")


def _sale(**kw):
    terms = C.SaleTerms(token_asset="aa" * 32,
                        payment_asset="2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de",
                        price_num=kw.get("price_num", 25), price_den=100,
                        treasury_prog="11" * 32, min_lot=kw.get("min_lot", 100000),
                        close_locktime=kw.get("close", 2_000_000_000),
                        reclaim_xonly="22" * 32, total_atoms=10**13)
    s = S.Sale("p", terms, "issuer")
    s.confirm_lock("ab" * 32, 0, s.script_pubkey, 10**13, "aa" * 32)
    return s


def test_cap_is_cumulative_per_sale(t):
    s = _sale()
    tier = T.TierPolicy().for_stake(FLOOR)          # 1,000 USDX cap
    cap = tier.cap_atoms
    plan = s.plan_buy("buyer", tier, payment_atoms=cap, height=1)
    t.eq(plan.payment_atoms <= cap, True, "a full-cap buy is allowed")
    s.record_purchase("buyer", plan.payment_atoms, plan.token_atoms)
    try:
        s.plan_buy("buyer", tier, payment_atoms=cap, height=1)
        t.ok(False, "a second full-cap buy should be refused")
    except S.CapExceeded:
        t.ok(True, "the cap counts everything already committed")


def test_a_visitor_cannot_invest(t):
    s = _sale()
    tier = T.TierPolicy().for_stake(0)
    try:
        s.plan_buy("nobody", tier, token_atoms=10**8, height=1)
        t.ok(False, "a visitor should not be able to invest")
    except S.SaleError:
        t.ok(True, "a visitor cannot invest")


def test_dust_remainder_is_refused(t):
    """The covenant will not leave a remainder below the minimum lot, so Levo
    must not plan a buy that would."""
    s = _sale(min_lot=1000)
    tier = T.TierPolicy().for_stake(25 * FLOOR)
    try:
        s.plan_buy("buyer", tier, token_atoms=10**13 - 1, height=1)
        t.ok(False, "should refuse to leave dust")
    except S.SaleError as e:
        t.ok("minimum lot" in str(e), "refuses a buy that would leave dust")


def test_closed_sales_do_not_sell(t):
    s = _sale(close=1000)          # a height-based close
    tier = T.TierPolicy().for_stake(25 * FLOOR)
    s.plan_buy("buyer", tier, token_atoms=10**8, height=999)
    t.ok(True, "sells before the close height")
    try:
        s.plan_buy("buyer", tier, token_atoms=10**8, height=1000)
        t.ok(False, "should not sell at or past the close")
    except S.SaleError:
        t.ok(True, "stops selling at the close height")


def test_reorged_funding_takes_the_sale_down(t):
    """Sequentia reorgs whenever its Bitcoin anchor does, so a lock can be
    un-made after the sale was shown as live. It must not stay on display."""
    s = _sale()
    t.eq(s.status, S.LIVE, "live once locked")
    s.mark_ghost()
    t.eq(s.status, S.GHOST, "a reorged lock makes the sale a ghost")
    t.eq(s.locked_atoms, 0, "and it holds nothing")
    tier = T.TierPolicy().for_stake(25 * FLOOR)
    try:
        s.plan_buy("buyer", tier, token_atoms=10**8, height=1)
        t.ok(False, "a ghost sale must not sell")
    except S.SaleError:
        t.ok(True, "a ghost sale cannot be bought")


def test_tiers_are_operator_configurable(t):
    """A deployment whose stakers all sit near the protocol floor needs
    different thresholds from mainnet defaults, and that is an operator's
    decision rather than a code change. Whatever is configured is also what the
    interface tells users, because the tier table comes from here."""
    import json
    spec = json.dumps([
        {"name": "Visitor", "min_stake": 0, "cap": 0, "may_list": False},
        {"name": "Backer", "min_stake": 45000, "cap": 500, "may_list": False},
        {"name": "Founder", "min_stake": 50000, "cap": 5000, "may_list": True},
    ])
    tiers = T.tiers_from_env({"LEVOD_TIERS": spec})
    p = T.TierPolicy(tiers)
    t.eq(p.for_stake(0).name, "Visitor", "configured floor tier")
    t.eq(p.for_stake(45000 * SEQ).name, "Backer", "configured middle tier")
    t.eq(p.for_stake(50000 * SEQ).may_list, True, "configured top tier may list")
    t.eq(p.for_stake(50000 * SEQ).cap_atoms, 5000 * 10**8, "caps convert to atoms")
    t.eq(T.tiers_from_env({}), None, "no configuration means the defaults")


def test_bad_tier_configuration_is_refused(t):
    """Silently accepting a broken tier table would either lock everybody out of
    listing or leave a staker with no tier at all."""
    import json
    for label, spec in [
        ("nobody can list", [{"name": "A", "min_stake": 0, "cap": 1}]),
        ("no zero-stake tier", [{"name": "A", "min_stake": 10, "cap": 1,
                                 "may_list": True}]),
        ("empty", []),
    ]:
        try:
            T.tiers_from_env({"LEVOD_TIERS": json.dumps(spec)})
            t.ok(False, "should refuse: %s" % label)
        except ValueError:
            t.ok(True, "refuses a tier table where %s" % label)


def test_default_tiers_are_shares_of_supply(t):
    """Not round numbers picked to feel right: 40,000 SEQ is 0.01% of the
    400,000,000 supply, which is where the protocol's own floor comes from."""
    supply = 400_000_000 * SEQ
    p = T.TierPolicy()
    shares = {x.name: x.min_stake_atoms / supply for x in p.tiers}
    t.eq(round(shares["Contributor"] * 100, 4), 0.01, "Contributor is 0.01%")
    t.eq(round(shares["Backer"] * 100, 4), 0.05, "Backer is 0.05%")
    t.eq(round(shares["Founder"] * 100, 4), 0.25, "Founder is 0.25%")
