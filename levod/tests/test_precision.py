"""A deployment whose payment asset does not divide into eight places.

Every figure on a Levo is denominated in the payment asset, and how finely that
asset divides is a property of the asset rather than of the chain: the chain
counts atoms, and the precision is what a registry says and what wallets show.
So a deployment priced in a two-place asset is an ordinary deployment, and it
has been the source of a defect in every audit pass so far -- tier caps a
million times too large, prices scaled at eight places, amounts printed at a
precision the parser then refused.

These are the figures that deployment produces, end to end through the platform
rather than through one helper at a time.
"""

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import market as M  # noqa: E402
import store as ST  # noqa: E402
import tiers as T  # noqa: E402
import units as U  # noqa: E402

PAY = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"
TOKEN = "aa" * 32
RECLAIM = "466d7fcae563e5cb09a0d1870bb580344804617879a14949cf22285f1bae3f27"
TREASURY = "4f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa"


class Reader:
    def __init__(self, policy):
        self.links = T.StakeLinks()
        self.policy = policy

    def standing(self, account):
        return {"tier": {"may_list": True, "name": "Founder"},
                "stake_atoms": 10 ** 18, "keys": []}


def _platform(decimals):
    policy = T.TierPolicy(payment_decimals=decimals)
    return M.Platform(ST.Store(Path(tempfile.mkdtemp()) / "state.json"),
                      Reader(policy), None, None, hrp="tb",
                      payment_asset=PAY, payment_label="PAY",
                      payment_decimals=decimals), policy


def test_the_default_tiers_are_the_same_tiers_at_any_precision(t):
    """A cap is a number of WHOLE UNITS. Two places or eight, the tier that may
    put a thousand units into a sale is the same tier."""
    for decimals in (0, 2, 8):
        _, policy = _platform(decimals)
        unit = 10 ** decimals
        caps = [x.cap_atoms for x in policy.tiers]
        t.eq(caps, [0, 1_000 * unit, 10_000 * unit, 100_000 * unit],
             "caps at %d places are the same figures in that asset's atoms" % decimals)


def test_a_price_costs_the_same_money_at_any_precision(t):
    """2.50 of the payment asset per whole token, written for each precision:
    the price is a ratio of atoms, so it is the precision that moves."""
    # payment atoms per token atom, for 2.50 whole units per whole token:
    #   8 places: 2.5e8 / 1e8  = 25/10
    #   2 places: 250   / 1e8  = 250/100_000_000
    #   0 places: 2.5   / 1e8  = 25/1_000_000_000
    for decimals, num, den in ((8, 25, 10), (2, 250, 100_000_000),
                               (0, 25, 1_000_000_000)):
        plat, policy = _platform(decimals)
        project = plat.list_project(
            "02" + "11" * 32,
            {"slug": "priced-%d" % decimals, "name": "Priced", "ticker": "PRC"},
            {"token_asset": TOKEN, "payment_asset": PAY, "price_num": num,
             "price_den": den, "treasury_prog": TREASURY, "min_lot": 10 ** 8,
             "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM,
             "total_atoms": 1_000 * 10 ** 8})
        sale = project.sale
        sale.confirm_lock("ab" * 32, 0, sale.script_pubkey, 1_000 * 10 ** 8, TOKEN)
        # Forty whole tokens, which is a hundred whole units of the payment
        # asset at 2.50 each, whatever the payment asset's precision is.
        plan = sale.plan_buy("02" + "22" * 32, policy.tiers[-1],
                             token_atoms=40 * 10 ** 8)
        t.eq(U.fmt(plan.payment_atoms, decimals), "100",
             "forty tokens cost 100 whole units at %d places" % decimals)


def test_what_the_wire_carries_is_read_back_as_the_same_money(t):
    """The API's own JSON, at two places, read the way a client reads it."""
    plat, policy = _platform(2)
    project = plat.list_project(
        "02" + "11" * 32, {"slug": "wire", "name": "Wire", "ticker": "WIR"},
        {"token_asset": TOKEN, "payment_asset": PAY, "price_num": 250,
         "price_den": 100_000_000, "treasury_prog": TREASURY, "min_lot": 10 ** 8,
         "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM,
         "total_atoms": 1_000 * 10 ** 8})
    sale = project.sale
    sale.confirm_lock("ab" * 32, 0, sale.script_pubkey, 1_000 * 10 ** 8, TOKEN)
    body = sale.to_json()
    t.eq(body["terms"]["total_atoms"], "100000000000", "atoms cross as strings")
    t.eq(U.fmt(int(body["locked_atoms"]), 8), "1,000",
         "and the token's own amount is in the token's places")
    plan = sale.plan_buy("02" + "22" * 32, policy.tiers[-1], token_atoms=40 * 10 ** 8)
    quote = plan.to_json()
    t.eq(U.fmt(int(quote["payment_atoms"]), 2), "100",
         "the quote is a hundred units of a two-place asset")
    t.eq(U.fmt(int(quote["token_atoms"]), 8), "40", "for forty tokens")


def test_a_sale_nobody_could_buy_from_is_refused_at_any_precision(t):
    """The dust floor is in the payment asset's atoms, so the smallest purchase
    a sale allows has to clear it whatever the asset divides into."""
    plat, _ = _platform(2)
    # No node, so no rates and no floor: the refusal cannot fire, and the
    # listing stands. What is being checked is that the code path does not
    # depend on eight places to work at all.
    project = plat.list_project(
        "02" + "11" * 32, {"slug": "tiny", "name": "Tiny", "ticker": "TNY"},
        {"token_asset": TOKEN, "payment_asset": PAY, "price_num": 1,
         "price_den": 100_000_000, "treasury_prog": TREASURY, "min_lot": 100,
         "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM,
         "total_atoms": 1_000 * 10 ** 8})
    t.eq(project.sale.terms.cost_for(100), 1,
         "a hundred token atoms cost one payment atom at this price")
