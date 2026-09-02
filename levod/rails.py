"""Payment rails: what a buyer can pay with, and what actually settles.

Levo accepts USDX and BTC. They are not symmetrical, and the difference is
worth stating plainly rather than smoothing over in the interface.

**USDX settles inside the covenant.** The sell leaf reads the USDX output with
transaction introspection and refuses the spend unless the project has been
paid. Buyer and project exchange in one transaction, and no third party can
interpose. The guarantee is consensus.

**BTC is native Bitcoin on the parent chain.** It is not a token on Sequentia
and not a pegged claim on one, so a Sequentia covenant cannot read a Bitcoin
output and BTC cannot be the asset the sell leaf checks. A BTC purchase is
therefore two steps: swap BTC for USDX on the rail the ecosystem already runs
(Lightning, atomic in itself), then fill the covenant with the USDX.

Levo does not stand in the middle of that. It does not take custody of BTC, it
does not hold an inventory of USDX, and it does not run the swap: the buyer's
own wallet does, the same way it swaps for any other asset. What Levo supplies
is the quote, the amount that has to arrive, and the verification afterwards.

The consequence, stated honestly because a buyer should be able to plan around
it: between the two steps the buyer is holding USDX. If they stop there they
have swapped BTC for a stablecoin at a quoted rate, which is not a loss and not
a position anybody else controls. A buyer who wants the consensus guarantee end
to end, with nothing between the legs, pays in USDX to begin with.

Rates come from the fee exchange-rate table of the node Levo reads: the same
table that node's mempool uses to price a fee paid in any asset. That table is
the node operator's policy, not a consensus fact, so a BTC quote is Levo's
quote. It is published with its source and its expiry so a buyer can check it
against their own wallet before swapping.
"""

import time

import units as U

USDX = "usdx"
BTC = "btc"

# The rate table prices assets against a reference unit of 1e-8 of a US
# dollar, so a rate of 1e8 is one dollar. SBTC is pegged bitcoin (1:1 BTC held
# by the sbtc-bridge reserve); its row is the node's price for a bitcoin, which
# is the only bitcoin price the table carries.
BTC_RATE_LABEL = "SBTC"


class RailUnavailable(RuntimeError):
    pass


class NodeRateSource:
    """The chain's fee exchange rates, cached briefly."""

    def __init__(self, rpc, ttl=30, btc_label=BTC_RATE_LABEL):
        self.rpc = rpc
        self.ttl = ttl
        self.btc_label = btc_label
        self._cached = None
        self._at = 0

    def rates(self):
        now = time.time()
        if self._cached is None or now - self._at > self.ttl:
            self._cached = self.rpc.call("getfeeexchangerates") or {}
            self._at = now
        return self._cached

    def payment_atoms_per_btc(self, payment_label="USDX"):
        """How many atoms of the payment asset one BTC is worth."""
        r = self.rates()
        btc = r.get(self.btc_label)
        pay = r.get(payment_label)
        if not btc or not pay:
            raise RailUnavailable(
                "the node does not price both %s and %s, so a BTC quote cannot "
                "be derived from the chain's own rates"
                % (self.btc_label, payment_label))
        # Both rates are reference units per whole unit, scaled by 1e8, so the
        # ratio is whole payment units per BTC; multiply out to atoms.
        per_btc = int(btc) * 100_000_000 // int(pay)
        if per_btc <= 0:
            raise RailUnavailable(
                "the node's rate table prices %s below one atom per bitcoin, "
                "so no BTC amount can be quoted" % payment_label)
        return per_btc


class Rails:
    def __init__(self, payment_asset, payment_label="USDX", rate_source=None,
                 quote_ttl=90):
        self.payment_asset = payment_asset
        self.payment_label = payment_label
        self.rate_source = rate_source
        self.quote_ttl = quote_ttl

    def _btc_ready(self):
        if self.rate_source is None:
            return False, "no rate source is configured"
        try:
            self.rate_source.payment_atoms_per_btc(self.payment_label)
            return True, None
        except Exception as e:
            return False, str(e)

    def available(self):
        ok, why = self._btc_ready()
        return [
            {"id": USDX, "label": self.payment_label, "available": True,
             "asset": self.payment_asset,
             "settles": "inside the covenant",
             "atomic": "end to end, enforced by consensus",
             "steps": 1},
            {"id": BTC, "label": "BTC", "available": ok,
             "unavailable_because": None if ok else why,
             "settles": "swapped to %s over Lightning, then into the covenant"
                        % self.payment_label,
             "atomic": "each step is atomic; between them you hold %s"
                       % self.payment_label,
             "steps": 2},
        ]

    def quote(self, rail, payment_atoms):
        """What a buyer sends, for a purchase that must deliver `payment_atoms`
        of the covenant's payment asset."""
        payment_atoms = int(payment_atoms)
        now = int(time.time())
        rail = str(rail or "").strip().lower()
        if rail in (USDX, "", self.payment_label.lower()):
            return {
                "rail": USDX, "send_asset": self.payment_asset,
                "send_label": self.payment_label,
                "send_atoms": payment_atoms,
                "delivers_atoms": payment_atoms,
                "rate": None, "taken_at": now, "expires_at": None,
                "steps": ["pay this amount straight into the covenant, in the "
                          "same transaction that hands you your tokens"],
            }
        if rail != BTC:
            raise RailUnavailable("unknown rail %r" % rail)
        ok, why = self._btc_ready()
        if not ok:
            raise RailUnavailable("the BTC rail is unavailable: %s" % why)
        per_btc = self.rate_source.payment_atoms_per_btc(self.payment_label)
        # Round the buyer's side UP so a rate that moves against them by a
        # rounding step does not leave the purchase short.
        sats = -(-payment_atoms * 100_000_000 // per_btc)
        return {
            "rail": BTC, "send_asset": "BTC", "send_label": "BTC",
            "send_sats": sats,
            "send_btc": sats / 100_000_000,
            "delivers_atoms": payment_atoms,
            "rate": {"payment_atoms_per_btc": per_btc,
                     "source": "the fee exchange-rate table of the node Levo reads "
                               "(getfeeexchangerates): the operator's table, not a "
                               "consensus price"},
            "taken_at": now, "expires_at": now + self.quote_ttl,
            "steps": [
                "swap %s BTC for %s over Lightning in your own wallet's swap"
                % (_btc(sats), U.fmt(payment_atoms, 8, self.payment_label)),
                "fill the covenant with that %s, which is the step that hands "
                "you your tokens" % self.payment_label,
            ],
            "note": "Levo takes no custody of either step. Between them you hold "
                    "%s, and you can stop there." % self.payment_label,
        }


def _btc(sats):
    return U.fmt(int(sats), 8)
