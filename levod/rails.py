"""Payment rails: what a buyer can pay with, and what actually settles.

Levo accepts BTC and USDX. Those two are not symmetrical, and the difference is
worth stating plainly rather than smoothing over in the UI.

USDX is a Sequentia asset, so it settles INSIDE the covenant. The sell leaf
reads the USDX output with transaction introspection and refuses the spend
unless the project has been paid. The guarantee is consensus: buyer and project
exchange in one transaction, and no third party can interpose.

BTC is native Bitcoin on the parent chain, not a token on Sequentia and not a
pegged claim on one. A Sequentia covenant cannot introspect a Bitcoin output, so
BTC cannot be the asset the sell leaf checks. A BTC purchase therefore settles
in two linked legs: the buyer pays BTC over Lightning, and the same preimage
releases the Sequentia leg that fills the covenant in USDX. This is the
cross-chain submarine swap the ecosystem already runs, not new machinery.

What that means for a buyer, said honestly:

  * Paying in USDX is atomic end to end.
  * Paying in BTC is atomic per leg, and the seam between the legs is held by
    the hash preimage. A buyer who wants the consensus guarantee with nothing
    at the seam pays in USDX.

`quote` prices a BTC purchase in the covenant's own payment asset, because that
is the amount that has to arrive for the sale to fill. The rate is a market
input, not a Levo opinion, so it is fetched rather than invented and every quote
carries the rate and the moment it was taken.
"""

import time

USDX = "usdx"
BTC = "btc"


class RailUnavailable(RuntimeError):
    pass


class Rails:
    """Which rails are usable right now, and what a purchase costs on each."""

    def __init__(self, payment_asset, rate_source=None, quote_ttl=60):
        self.payment_asset = payment_asset
        self.rate_source = rate_source      # callable() -> payment atoms per BTC
        self.quote_ttl = quote_ttl

    def available(self):
        rails = [{
            "id": USDX,
            "label": "USDX",
            "settles": "in the covenant",
            "atomic": "end to end, enforced by consensus",
            "available": True,
        }, {
            "id": BTC,
            "label": "BTC",
            "settles": "over Lightning, linked to the covenant fill by a hash preimage",
            "atomic": "per leg; the preimage links them",
            "available": self.rate_source is not None,
        }]
        return rails

    def quote(self, rail, payment_atoms):
        """What the buyer sends, for a purchase that must deliver `payment_atoms`
        of the covenant's payment asset."""
        if rail == USDX:
            return {"rail": USDX, "send_asset": self.payment_asset,
                    "send_atoms": int(payment_atoms),
                    "delivers_atoms": int(payment_atoms),
                    "rate": None, "taken_at": int(time.time()),
                    "expires_at": None,
                    "note": "paid directly into the covenant"}
        if rail != BTC:
            raise RailUnavailable("unknown rail %r" % rail)
        if self.rate_source is None:
            raise RailUnavailable(
                "the BTC rail needs a live rate and a Lightning swap provider; "
                "neither is configured on this deployment")
        rate = self.rate_source()          # payment-asset atoms per 1 BTC
        if not rate or rate <= 0:
            raise RailUnavailable("no usable BTC rate")
        sats = -(-int(payment_atoms) * 100_000_000 // int(rate))   # ceil
        now = int(time.time())
        return {"rail": BTC, "send_asset": "BTC", "send_sats": sats,
                "delivers_atoms": int(payment_atoms),
                "rate": rate, "taken_at": now,
                "expires_at": now + self.quote_ttl,
                "note": "pay the Lightning invoice; the preimage releases the "
                        "covenant fill that delivers your tokens"}
