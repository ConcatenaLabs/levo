"""Tier checks: thresholds, proof of control, and the sale rules around them."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import sale as S  # noqa: E402
import tiers as T  # noqa: E402
import units as U  # noqa: E402

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
    """A node that answers by controller, as an upgraded one does."""

    def __init__(self, weights, delegated=None, supported=True):
        self._w = weights
        self._d = delegated or {}
        self._supported = supported

    def staker_weights(self):
        return dict(self._w)

    def controller_weights(self):
        if not self._supported:
            return ({k: {"weight_atoms": v, "delegated": False, "signer": None}
                     for k, v in self._w.items()}, False)
        return ({k: {"weight_atoms": v,
                     "delegated": k in self._d,
                     "signer": self._d.get(k)}
                 for k, v in self._w.items()}, True)


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


def test_the_login_key_counts_without_a_separate_link(t):
    """Linking proves control of a key you did NOT sign in with. If the key you
    signed in with is itself a staker, the login already proved it."""
    me = "02" + "55" * 32
    reader = T.StakeReader(_FakeRPC({me: 3 * FLOOR}), T.StakeLinks())
    st = reader.standing(me)
    t.eq(st["stake_atoms"], 3 * FLOOR, "the login key's own stake counts")
    t.eq(st["keys"][0]["is_login_key"], True, "and is marked as such")

    # It must not be double counted if it is also linked explicitly.
    links = T.StakeLinks()
    links.link(me, me)
    reader2 = T.StakeReader(_FakeRPC({me: 3 * FLOOR}), links)
    t.eq(reader2.standing(me)["stake_atoms"], 3 * FLOOR,
         "and is never counted twice")

    # An account that is not a staker still gets nothing.
    t.eq(T.StakeReader(_FakeRPC({}), T.StakeLinks()).standing(me)["stake_atoms"], 0,
         "a login key with no stake adds nothing")


def test_delegated_stake_counts_for_the_person_who_owns_it(t):
    """Delegation lends block-signing rights, never the coins: only the
    controller can ever spend them. The chain's default view is keyed by signer,
    under which a delegator appears to have nothing at all -- so Levo asks by
    controller instead."""
    me = "02" + "77" * 32
    pool = "03" + "88" * 32
    rpc = _FakeRPC({me: 5 * FLOOR}, delegated={me: pool})
    st = T.StakeReader(rpc, T.StakeLinks()).standing(me)
    t.eq(st["stake_atoms"], 5 * FLOOR, "a delegated stake still counts for its owner")
    t.eq(st["tier"]["name"], "Backer", "and still earns its tier")
    t.eq(st["keys"][0]["delegated"], True, "the interface can say it is delegated")
    t.eq(st["keys"][0]["delegated_to"], pool, "and to whom")
    t.eq(st["counts_delegated_stake"], True, "the node answered by controller")


def test_a_pool_is_not_credited_with_its_depositors_stake(t):
    """Otherwise a tier could be earned with money somebody else put up, by
    whoever happened to be trusted with the signing."""
    pool = "03" + "88" * 32
    depositor = "02" + "77" * 32
    # By controller, the pool holds only its own stake.
    rpc = _FakeRPC({pool: FLOOR, depositor: 25 * FLOOR},
                   delegated={depositor: pool})
    st = T.StakeReader(rpc, T.StakeLinks()).standing(pool)
    t.eq(st["stake_atoms"], FLOOR, "the pool counts only what it staked itself")
    t.eq(st["tier"]["name"], "Contributor", "so its tier is its own")


def test_an_old_node_says_so_rather_than_reporting_no_stake(t):
    """A node that cannot answer by controller reports a delegator as having
    nothing. Silently showing that as zero would lock them out with no reason
    given, so the standing carries the explanation."""
    me = "02" + "77" * 32
    rpc = _FakeRPC({me: 3 * FLOOR}, supported=False)
    st = T.StakeReader(rpc, T.StakeLinks()).standing(me)
    t.eq(st["counts_delegated_stake"], False, "flagged as the signer-keyed view")
    t.ok("delegated" in st["delegation_note"], "and explains what that costs")


def test_tier_tables_are_checked_for_sense(t):
    def bad(spec, needle):
        try:
            T.tiers_from_env({"LEVOD_TIERS": json.dumps(spec)})
            t.ok(False, "refuses " + needle)
        except ValueError as e:
            t.ok(needle in str(e), "refuses " + needle, str(e))

    import json
    bad([{"name": "V", "min_stake": 0, "cap": 0}, {"name": "A", "min_stake": 10, "cap": 5, "may_list": True},
         {"name": "B", "min_stake": 10, "cap": 6}], "distinct")
    bad([{"name": "V", "min_stake": 0, "cap": 0}, {"name": "A", "min_stake": 10, "cap": -5, "may_list": True}],
        "negative")
    bad([{"name": "V", "min_stake": 0, "cap": 0}, {"name": "A", "min_stake": 10, "cap": 50, "may_list": True},
         {"name": "B", "min_stake": 20, "cap": 40}], "smaller cap")


def test_the_listing_tier_is_the_lowest_that_may_list(t):
    import json
    tiers = T.tiers_from_env({"LEVOD_TIERS": json.dumps([
        {"name": "V", "min_stake": 0, "cap": 0},
        {"name": "Mid", "min_stake": 10, "cap": 50, "may_list": True},
        {"name": "Top", "min_stake": 20, "cap": 60}])})
    p = T.TierPolicy(tiers)
    t.eq(p.listing_tier().name, "Mid", "the tier named in a listing refusal is the one that may list")
    t.eq(p.for_stake(20 * SEQ).may_list, False, "a higher tier without may_list cannot list")


def test_signing_in_with_a_linked_key_moves_it(t):
    """One stake, one account. If a staking key was linked to account X and
    its holder then signs in with the key itself, the newest proof of control
    wins: the key counts for the login and no longer for X."""
    class RPC:
        def controller_weights(self):
            return {"02" + "aa" * 32: {"weight_atoms": FLOOR, "delegated": False, "signer": None}}, True

    links = T.StakeLinks()
    links.link("02" + "bb" * 32, "02" + "aa" * 32)
    r = T.StakeReader(RPC(), links)
    t.eq(r.standing("02" + "bb" * 32)["stake_atoms"], FLOOR, "the link counts for X at first")
    st = r.standing("02" + "aa" * 32)
    t.eq(st["stake_atoms"], FLOOR, "signing in with the key counts it for the login")
    t.eq(links.owner_of("02" + "aa" * 32), "02" + "aa" * 32, "and the key moved")
    t.eq(links.dirty, True, "which is flagged for saving")
    t.eq(r.standing("02" + "bb" * 32)["stake_atoms"], 0, "so X no longer has it")


def test_an_offer_never_buys_more_than_it_offered(t):
    """Offering an amount to spend converts to tokens by rounding DOWN.

    The covenant charges the CEILING of the price, so rounding the other way
    quotes a purchase costing more than was offered, and the buyer is then told
    their own funds are short of the number they just typed. Prices that do not
    divide evenly are where the two roundings disagree.
    """
    top = T.TierPolicy().tiers[-1]
    for num, den in ((3, 2), (7, 3), (1, 4), (5, 5)):
        s = _sale(price_num=num, min_lot=1)
        s.terms.price_num, s.terms.price_den = num, den
        for offer in (100, 1000, 12345, 10 ** 8):
            plan = s.plan_buy("buyer-%d-%d-%d" % (num, den, offer), top,
                              payment_atoms=offer, height=1)
            cost = s.terms.cost_for(plan.token_atoms)
            t.ok(cost <= offer,
                 "a %d/%d sale never charges more than the %d offered" % (num, den, offer),
                 "%d tokens cost %d" % (plan.token_atoms, cost))


def test_a_printed_amount_is_one_the_tools_accept_back(t):
    """`levo` prints amounts a person is meant to paste back -- into a node's
    RPC, or into `levo record`. An amount printed at a precision the parser
    then refuses is a command this platform produced and rejects."""
    import importlib.util
    src = open(str(Path(__file__).resolve().parent.parent.parent / "bin" / "levo")).read()
    ns = {"__file__": "bin/levo"}
    exec(compile(src.split("def main(")[0], "bin/levo", "exec"), ns)
    units = ns["_units"]
    for decimals in (0, 2, 8):
        for atoms in (1, 1000, 123456789):
            printed = units(atoms, decimals)
            t.eq(U.parse(printed, decimals), atoms,
                 "%s at %d places parses back" % (printed, decimals))


def test_an_account_holds_a_bounded_number_of_staking_keys(t):
    """Every link rewrites the whole state file and every reading of an
    account's standing asks the chain about every key it holds, so an account
    that could link keys in a loop is an account that costs more the longer it
    runs."""
    links = T.StakeLinks()
    acct = "02" + "11" * 32
    for i in range(T.MAX_LINKED_KEYS):
        links.link(acct, "02%062x" % i)
    t.eq(len(links.keys_for(acct)), T.MAX_LINKED_KEYS, "it holds the keys it linked")
    try:
        links.link(acct, "02%062x" % 9999)
        t.ok(False, "one more is refused")
    except ValueError as e:
        t.ok("Unlink one" in str(e), "one more is refused, with what to do", str(e))
    # Re-linking one it already holds is not a new key and stays allowed.
    links.link(acct, "02%062x" % 0)
    t.eq(len(links.keys_for(acct)), T.MAX_LINKED_KEYS, "re-linking a key it has is fine")
    # A state file written before the cap is read rather than refused: the
    # keys are real, and refusing to start would be a self-inflicted outage.
    over = {acct: ["02%062x" % i for i in range(T.MAX_LINKED_KEYS + 5)]}
    fresh = T.StakeLinks()
    fresh.load(over)
    t.eq(len(fresh.keys_for(acct)), T.MAX_LINKED_KEYS + 5,
         "a file written before the cap loads as it is")
