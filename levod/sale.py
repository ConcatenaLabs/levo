"""Sale lifecycle: lock, sell, reclaim -- and what Levo is allowed to promise.

A Levo sale has exactly three moments that matter, and the chain decides all
three.

  LOCK      The project sends its whole sale allocation to the covenant address
            Levo derives from the published terms. Until that output exists and
            matches, the sale is a draft and Levo will not show it as live.
            Nobody takes the project's word for the lock: the address IS the
            terms, so a funded output that verifies is proof.

  SELL      A buyer spends the covenant, paying the treasury and taking tokens
            in ONE transaction. There is no moment where the project has been
            paid and the buyer has not been delivered, and no moment where Levo
            holds either side. A partial buy re-rests the unsold remainder at
            the identical address, so the sale continues without the project
            doing anything.

  RECLAIM   After the close locktime the project sweeps whatever did not sell.

What the covenant enforces, and therefore what Levo can promise absolutely:
the price, the treasury that gets paid, the token being sold, the minimum lot,
and the close date. None of these can be changed once the sale is funded, by the
project or by Levo.

What the covenant does NOT enforce, and therefore what Levo must not overstate:
a per-buyer maximum. The sell leaf has a floor (`min_lot`) but no ceiling, and
it is permissionless by design -- anybody who can build the transaction may buy,
whether or not they came through Levo. Tier caps are a platform allocation
policy, honestly enforced on every purchase Levo plans, and they are not a
consensus rule. `doc/tiers-are-policy.md` says so in the same words, and the API
returns `enforced_by` on every cap so a client cannot accidentally imply more.
"""

import time

import covenant as C


DRAFT = "draft"          # terms fixed, address derived, tokens not yet locked
LIVE = "live"            # funded and verified, selling
PARTIAL = "partial"      # some sold, remainder re-rested at the same address
SOLD_OUT = "sold_out"    # covenant spent with no remainder
CLOSED = "closed"        # past the close locktime; reclaimable by the project
GHOST = "ghost"          # funding undone by a Bitcoin-driven reorg


class SaleError(ValueError):
    pass


class CapExceeded(SaleError):
    def __init__(self, msg, allowance_atoms):
        super().__init__(msg)
        self.allowance_atoms = allowance_atoms


class Sale:
    """One project's token sale."""

    def __init__(self, project_id, terms, issuer_account, created_at=None):
        self.project_id = project_id
        self.terms = terms
        self.issuer_account = issuer_account
        self.created_at = created_at or int(time.time())
        self.cov = C.derive(terms)
        self.status = DRAFT
        self.funding = None        # {"txid":..., "vout":..., "atoms":...}
        self.locked_atoms = 0      # what the covenant currently holds
        self.sold_atoms = 0
        self.allocations = {}      # account pubkey -> payment atoms committed

    # --- addresses and terms ------------------------------------------------

    @property
    def script_pubkey(self):
        return self.cov.spk_hex

    def verify_funding_spk(self, onchain_spk):
        return self.cov.verify_funding(onchain_spk)

    # --- lock ---------------------------------------------------------------

    def confirm_lock(self, txid, vout, spk_hex, value_atoms, asset_hex,
                     blinded=False):
        """Accept an on-chain output as this sale's lock, or refuse it.

        Every one of these checks has a way to lose money if skipped, so none of
        them is a formality: a wrong scriptPubKey means the tokens are not in
        the covenant the terms describe, a wrong asset means the sale would sell
        something other than what it advertises, and a short value means the
        sale cannot deliver the allocation it published.
        """
        self.verify_funding_spk(spk_hex)
        if blinded or asset_hex is None or value_atoms is None:
            # A confidential output states nothing; it commits. The sell leaf
            # reads the value it is spending and refuses anything it cannot
            # read, so tokens locked into a blinded output are locked FOREVER --
            # no buy can ever spend them and no reclaim can either. Refusing
            # loudly here is the only chance to stop that.
            raise SaleError(
                "that output is confidential, and a sale covenant can only hold "
                "an explicit one: the sell leaf reads the amount it is spending, "
                "so tokens locked into a blinded output could never be sold OR "
                "reclaimed. Send the tokens to the sale address from an "
                "unblinded output and confirm that instead.")
        if str(asset_hex).lower() != self.terms.token_asset:
            raise SaleError("locked output holds asset %s, but the sale sells %s"
                            % (asset_hex, self.terms.token_asset))
        value_atoms = int(value_atoms)
        if self.terms.total_atoms is not None and value_atoms < self.terms.total_atoms:
            raise SaleError("locked %d atoms, short of the published allocation of %d"
                            % (value_atoms, self.terms.total_atoms))
        if value_atoms < self.terms.min_lot:
            raise SaleError("locked amount is below the sale's own minimum lot")
        self.terms.assert_no_overflow(value_atoms)
        self.funding = {"txid": txid, "vout": int(vout), "atoms": value_atoms}
        self.locked_atoms = value_atoms
        self.status = LIVE
        return True

    # --- sell ---------------------------------------------------------------

    def remaining_atoms(self):
        return self.locked_atoms

    def is_open(self, height=None, now=None):
        if self.status not in (LIVE, PARTIAL):
            return False
        return not self.has_closed(height=height, now=now)

    def has_closed(self, height=None, now=None):
        """The close is an absolute locktime, so it is a HEIGHT below 500000000
        and a unix TIME at or above it -- the same rule the chain applies when
        it evaluates the reclaim leaf's CHECKLOCKTIMEVERIFY."""
        lt = self.terms.close_locktime
        if lt < 500_000_000:
            return height is not None and height >= lt
        return (now or time.time()) >= lt

    def allocation_remaining(self, account, tier):
        return self.allowance_for(account, tier)

    def allowance_for(self, account, tier):
        """Payment atoms this account may still commit to THIS sale."""
        if not tier.cap_atoms:
            return 0
        return max(0, tier.cap_atoms - self.allocations.get(account, 0))

    def plan_buy(self, account, tier, token_atoms=None, payment_atoms=None,
                 height=None, now=None):
        """Work out the exact purchase, or explain precisely why it cannot happen.

        Either ask for a number of tokens or offer an amount of payment; the
        other side is derived at the covenant's own ceiling price, because that
        is the arithmetic the leaf performs and quoting anything else would
        quote a purchase the chain rejects.
        """
        if not self.is_open(height=height, now=now):
            raise SaleError("this sale is not open (%s)" % self.status)
        if tier.cap_atoms <= 0:
            raise SaleError("your tier cannot invest; stake SEQ to reach a tier "
                            "with an allocation")

        locked = self.locked_atoms
        if locked <= 0:
            raise SaleError("the sale holds no tokens")

        if token_atoms is None and payment_atoms is None:
            raise SaleError("ask for a token amount or offer a payment amount")
        if token_atoms is None:
            # Most tokens obtainable without exceeding the offer, at ceil price.
            token_atoms = (int(payment_atoms) * self.terms.price_den) // self.terms.price_num

        token_atoms = int(token_atoms)
        if token_atoms < self.terms.min_lot:
            raise SaleError("minimum purchase is %d atoms" % self.terms.min_lot)
        if token_atoms > locked:
            raise SaleError("only %d atoms remain in this sale" % locked)

        remainder = locked - token_atoms
        if 0 < remainder < self.terms.min_lot:
            # The covenant refuses to leave dust behind, so the only valid buys
            # near the end are ones that clear the sale out.
            raise SaleError(
                "that would leave %d atoms behind, below the sale's minimum lot "
                "of %d; buy the remaining %d atoms to clear the sale"
                % (remainder, self.terms.min_lot, locked))

        cost = self.terms.cost_for(token_atoms)

        allowance = self.allowance_for(account, tier)
        if cost > allowance:
            raise CapExceeded(
                "this purchase costs %d payment atoms but your %s tier allows %d "
                "more in this sale" % (cost, tier.name, allowance), allowance)

        idx = 0 if self.funding is None else 0   # the covenant is input 0 in Levo's plan
        return BuyPlan(sale=self, account=account, token_atoms=token_atoms,
                       payment_atoms=cost, remainder_atoms=remainder,
                       covenant_input_index=idx)

    def record_purchase(self, account, payment_atoms, token_atoms):
        """Commit a purchase that has actually landed on chain."""
        self.allocations[account] = self.allocations.get(account, 0) + int(payment_atoms)
        self.locked_atoms -= int(token_atoms)
        self.sold_atoms += int(token_atoms)
        self.status = SOLD_OUT if self.locked_atoms == 0 else PARTIAL
        return self.status

    def mark_ghost(self):
        """The lock was undone by a Bitcoin-driven reorg.

        Sequentia reorgs whenever its Bitcoin anchor does, so a funding output
        can be un-made after Levo has already shown the sale as live. When that
        happens the sale is not 'briefly unconfirmed', it is not funded at all,
        and it goes back to being a draft rather than staying on display.
        """
        self.status = GHOST
        self.funding = None
        self.locked_atoms = 0

    # --- serialisation ------------------------------------------------------

    def to_json(self, height=None, now=None):
        return {
            "project_id": self.project_id,
            "issuer_account": self.issuer_account,
            "status": CLOSED if (self.status in (LIVE, PARTIAL)
                                 and self.has_closed(height=height, now=now))
                      else self.status,
            "script_pubkey": self.script_pubkey,
            "terms": self.terms.to_json(),
            "funding": self.funding,
            "locked_atoms": self.locked_atoms,
            "sold_atoms": self.sold_atoms,
            "created_at": self.created_at,
        }


class BuyPlan:
    """Exactly what the buyer's transaction must contain.

    Levo builds no transaction and signs nothing. It hands back the constraints
    the covenant will check, so the buyer's own wallet can construct a spend and
    satisfy them -- and so the buyer can verify, before signing, that the outputs
    are the ones the published terms imply.
    """

    def __init__(self, sale, account, token_atoms, payment_atoms,
                 remainder_atoms, covenant_input_index=0):
        self.sale = sale
        self.account = account
        self.token_atoms = token_atoms
        self.payment_atoms = payment_atoms
        self.remainder_atoms = remainder_atoms
        self.k = covenant_input_index

    def to_json(self):
        t = self.sale.terms
        outputs = [{
            "index": 2 * self.k,
            "role": "treasury payment",
            "asset": t.payment_asset,
            "min_atoms": self.payment_atoms,
            "script_pubkey_witness_v1_program": t.treasury_prog,
            "why": "the sell leaf checks this output pays the project at least "
                   "the ceiling price for what you take",
        }]
        if self.remainder_atoms:
            outputs.append({
                "index": 2 * self.k + 1,
                "role": "unsold remainder, re-rested",
                "asset": t.token_asset,
                "exact_atoms": self.remainder_atoms,
                "script_pubkey": self.sale.script_pubkey,
                "why": "the leaf requires the remainder to return to the "
                       "identical covenant, so the sale keeps resting",
            })
        return {
            "sale": self.sale.project_id,
            "buyer": self.account,
            "token_atoms": self.token_atoms,
            "payment_atoms": self.payment_atoms,
            "remainder_atoms": self.remainder_atoms,
            "covenant": {
                "input_index": self.k,
                "outpoint": self.sale.funding,
                "script_pubkey": self.sale.script_pubkey,
                "witness": [w.hex() if isinstance(w, (bytes, bytearray)) else w
                            for w in self.sale.cov.sell_witness()],
                "note": "the sell leaf needs no signature and no witness data "
                        "beyond the leaf and its control block",
            },
            "required_outputs": outputs,
            "your_tokens": {
                "asset": t.token_asset,
                "atoms": self.token_atoms,
                "why": "whatever the covenant does not re-rest is yours to "
                       "direct; put it at any output the required ones do not use",
            },
        }
