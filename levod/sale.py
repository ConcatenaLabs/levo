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
the price, the treasury that gets paid, the token that a remainder must be, the
minimum lot, and the earliest moment the project can reclaim. None of these can
be changed once the sale is funded, by the project or by Levo. Note what that
does and does not rule out: the treasury is the project's own key, so a project
can always buy its own sale out at the published price, on chain, for the cost
of the fee. It cannot take the tokens by any other route, and it cannot change
the terms a buyer sees.

What the covenant does NOT enforce, and therefore what Levo must not overstate:
a per-buyer maximum. The sell leaf has a floor (`min_lot`) but no ceiling, and
it is permissionless by design -- anybody who can build the transaction may buy,
whether or not they came through Levo. Tier caps are a platform allocation
policy, honestly enforced on every purchase Levo plans, and they are not a
consensus rule. `doc/tiers-are-policy.md` says so in the same words, and the API
returns `enforced_by` on every cap so a client cannot accidentally imply more.

Nor does the sell leaf carry the close: it has no locktime. The close opens the
reclaim path; it does not shut the sell path. Levo stops planning purchases at
the close, and a project should reclaim promptly, because until it does a buyer
who builds the transaction can still fill the sale.
"""

import time

import covenant as C
import units as U


DRAFT = "draft"          # terms fixed, address derived, tokens not yet locked
LIVE = "live"            # funded and verified, selling
PARTIAL = "partial"      # some sold, remainder re-rested at the same address
SOLD_OUT = "sold_out"    # covenant spent with no remainder, before the close
CLOSED = "closed"        # past the close locktime; reclaimable by the project
GHOST = "ghost"          # funding undone by a Bitcoin-driven reorg
RECLAIMED = "reclaimed"  # closed, and the project's reclaim has swept the rest

FINAL = (SOLD_OUT, RECLAIMED)


class SaleError(ValueError):
    pass


class CapExceeded(SaleError):
    def __init__(self, msg, allowance_atoms):
        super().__init__(msg)
        self.allowance_atoms = allowance_atoms


class Sale:
    """One project's token sale."""

    def __init__(self, project_id, terms, issuer_account, created_at=None,
                 token_label="", token_decimals=8, payment_label="", payment_decimals=8):
        self.project_id = project_id
        self.terms = terms
        self.issuer_account = issuer_account
        self.created_at = created_at or int(time.time())
        self.cov = C.derive(terms)
        self.status = DRAFT
        self.funding = None        # {"txid":..., "vout":..., "atoms":..., "height":..., "block":...}
        self.locked_atoms = 0      # what the covenant currently holds
        self.sold_atoms = 0
        self.allocations = {}      # account pubkey -> payment atoms committed
        self.purchases = {}        # account pubkey -> [{txid, token_atoms, payment_atoms, at, verified}]
        self.candidates = []       # outpoints a recorded purchase said the remainder rests at
        self.reclaim_txids = []    # reclaims Levo built for this sale, by txid
        self.strays = []           # other assets seen resting at the sale address
        # Labels for messages people read. Never used in arithmetic.
        self.token_label = token_label
        self.token_decimals = int(token_decimals)
        self.payment_label = payment_label
        self.payment_decimals = int(payment_decimals)

    # --- amounts for people ---------------------------------------------------

    def tokens(self, atoms):
        return U.fmt(atoms, self.token_decimals, self.token_label or "tokens")

    def payment(self, atoms):
        return U.fmt(atoms, self.payment_decimals, self.payment_label or "of the payment asset")

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
        if self.status not in (DRAFT, GHOST):
            raise SaleError(
                "this sale is already funded at %s:%s; a sale is locked once. "
                "The tokens you sent are resting at the sale address alongside "
                "it and are not for sale" % ((self.funding or {}).get("txid"),
                                            (self.funding or {}).get("vout")))
        self.verify_funding_spk(spk_hex)
        if blinded or asset_hex is None or value_atoms is None:
            # A confidential output states nothing; it commits. The sell leaf
            # reads the value it is spending and refuses anything it cannot
            # read, so tokens locked into a blinded output can never be sold.
            # (The reclaim leaf reads nothing, so the project could still take
            # them back after the close, with the blinding factors it holds.)
            # Refusing loudly here is the only chance to stop a dead sale.
            raise SaleError(
                "that output is confidential, and a sale covenant can only hold "
                "an explicit one: the sell leaf reads the amount it is spending, "
                "so tokens locked into a blinded output could never be sold, only "
                "taken back by you after the close. Send the tokens to the sale "
                "address as an unblinded output and confirm that instead.")
        if str(asset_hex).lower() != self.terms.token_asset:
            raise SaleError("the locked output holds asset %s, but the sale sells %s"
                            % (asset_hex, self.terms.token_asset))
        value_atoms = int(value_atoms)
        if self.terms.total_atoms is not None and value_atoms != self.terms.total_atoms:
            raise SaleError(
                "the locked output holds %s, but the sale publishes %s; send "
                "exactly the published allocation in one output"
                % (self.tokens(value_atoms), self.tokens(self.terms.total_atoms)))
        if value_atoms < self.terms.min_lot:
            raise SaleError("the locked amount is below the sale's own minimum lot")
        self.terms.assert_no_overflow(value_atoms)
        self.funding = {"txid": str(txid).lower(), "vout": int(vout), "atoms": value_atoms}
        self.locked_atoms = value_atoms
        self.sold_atoms = 0
        self.candidates = []
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
        it evaluates the reclaim leaf's CHECKLOCKTIMEVERIFY.

        For a time close, pass the chain's median time past as `now` when the
        answer has to match what the chain will accept (a reclaim); the wall
        clock is the conservative answer for a buyer.
        """
        lt = self.terms.close_locktime
        if lt < 500_000_000:
            return height is not None and height >= lt
        return (now or time.time()) >= lt

    def close_is_height(self):
        return self.terms.close_locktime < 500_000_000

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
            shown = CLOSED if (self.status in (LIVE, PARTIAL)
                               and self.has_closed(height=height, now=now)) else self.status
            raise SaleError("this sale is not open (%s)" % shown.replace("_", " "))
        if tier.cap_atoms <= 0:
            raise SaleError("your tier cannot invest; stake Sequence to reach a tier "
                            "with a cap")

        locked = self.locked_atoms
        if locked <= 0:
            raise SaleError("the sale holds no tokens")

        if token_atoms is None and payment_atoms is None:
            raise SaleError("ask for a token amount or offer a payment amount")
        if token_atoms is None:
            payment_atoms = _atoms(payment_atoms, "payment_atoms")
            # Most tokens obtainable without exceeding the offer, at ceil price.
            token_atoms = (payment_atoms * self.terms.price_den) // self.terms.price_num

        token_atoms = _atoms(token_atoms, "token_atoms")
        if token_atoms < self.terms.min_lot:
            raise SaleError("the minimum purchase is %s" % self.tokens(self.terms.min_lot))
        if token_atoms > locked:
            raise SaleError("only %s remain in this sale" % self.tokens(locked))

        remainder = locked - token_atoms
        if 0 < remainder < self.terms.min_lot:
            # The covenant refuses to leave dust behind, so the only valid buys
            # near the end are ones that clear the sale out.
            raise SaleError(
                "that would leave %s behind, below the sale's minimum lot of %s; "
                "buy the remaining %s to clear the sale"
                % (self.tokens(remainder), self.tokens(self.terms.min_lot),
                   self.tokens(locked)))

        cost = self.terms.cost_for(token_atoms)

        allowance = self.allowance_for(account, tier)
        if cost > allowance:
            raise CapExceeded(
                "this purchase costs %s, but your %s tier can still put %s into "
                "this sale" % (self.payment(cost), tier.name, self.payment(allowance)),
                allowance)

        # The covenant is input 0 in every transaction Levo builds.
        return BuyPlan(sale=self, account=account, token_atoms=token_atoms,
                       payment_atoms=cost, remainder_atoms=remainder,
                       covenant_input_index=0)

    def record_purchase(self, account, payment_atoms, token_atoms, txid=None,
                        verified=None, at=None):
        """Write a purchase to the allocation ledger.

        The ledger is what the per-buyer cap is measured against. It only ever
        grows: a purchase that happened cannot be un-happened by a later call,
        and a caller cannot lower their own commitment. What the sale holds is
        the watcher's business, read from the chain.
        """
        payment_atoms = _atoms(payment_atoms, "payment_atoms")
        token_atoms = _atoms(token_atoms, "token_atoms")
        if payment_atoms <= 0 or token_atoms <= 0:
            raise SaleError("a purchase has a positive token amount and a positive payment")
        self.allocations[account] = self.allocations.get(account, 0) + payment_atoms
        entry = {"txid": (str(txid).lower() if txid else None),
                 "token_atoms": token_atoms, "payment_atoms": payment_atoms,
                 "at": int(at or time.time()), "verified": verified}
        self.purchases.setdefault(account, []).append(entry)
        return entry

    def expect_remainder_at(self, txid, vout=1):
        """A purchase was made with this transaction; if it left a remainder,
        that remainder rests at its output 2k+1, which is 1 in Levo's layout.
        The watcher checks the outpoint with the mempool included, so the sale
        moves the moment the buy is broadcast."""
        if not txid:
            return
        cand = {"txid": str(txid).lower(), "vout": int(vout)}
        if cand not in self.candidates:
            self.candidates = (self.candidates + [cand])[-8:]

    def note_reclaim(self, txid):
        """A reclaim was built with this transaction id. Once its output 0
        shows up holding the sale token, the sale is proven reclaimed."""
        if txid and str(txid).lower() not in self.reclaim_txids:
            self.reclaim_txids = (self.reclaim_txids + [str(txid).lower()])[-8:]

    def mark_ghost(self):
        """The lock was undone by a Bitcoin-driven reorg.

        Sequentia reorgs whenever its Bitcoin anchor does, so a funding output
        can be un-made after Levo has already shown the sale as live. When that
        happens the sale is not 'briefly unconfirmed', it is not funded at all:
        it is shown as not funded and stops being investable until the project
        locks tokens at the same address again. Purchases that spent the
        un-made output went with it, so the ledger they filled is cleared.
        """
        self.status = GHOST
        self.funding = None
        self.locked_atoms = 0
        self.sold_atoms = 0
        self.candidates = []
        self.allocations = {}
        for entries in self.purchases.values():
            for e in entries:
                e["voided"] = True

    def mark_sold_out(self):
        """The covenant emptied before the close. Only buys can do that."""
        self.locked_atoms = 0
        if self.terms.total_atoms:
            self.sold_atoms = self.terms.total_atoms
        self.status = SOLD_OUT

    def mark_emptied(self, reclaimed=False):
        """The covenant emptied after the close.

        After the close both leaves can spend it -- a buyer may still fill it,
        and the project may reclaim it -- so absence alone does not say which.
        `reclaimed` is set only on positive evidence that the project's reclaim
        landed; otherwise the sale is closed with nothing left, which is true
        either way, and `sold_atoms` stays at what was last observed.
        """
        self.locked_atoms = 0
        self.status = RECLAIMED if reclaimed else CLOSED

    # --- serialisation ------------------------------------------------------

    def shown_status(self, height=None, now=None):
        if self.status in (LIVE, PARTIAL) and self.has_closed(height=height, now=now):
            return CLOSED
        return self.status

    def to_json(self, height=None, now=None):
        return {
            "project_id": self.project_id,
            "issuer_account": self.issuer_account,
            "status": self.shown_status(height=height, now=now),
            "script_pubkey": self.script_pubkey,
            "terms": self.terms.to_json(),
            "funding": self.funding,
            "locked_atoms": self.locked_atoms,
            "sold_atoms": self.sold_atoms,
            "buyers": len([a for a, v in self.allocations.items() if v > 0]),
            "reclaim_txids": list(self.reclaim_txids),
            "strays": list(self.strays),
            "created_at": self.created_at,
        }


def _atoms(v, name):
    """An atom count from JSON: an int, or a decimal string for counts that
    JavaScript cannot carry as a number."""
    if isinstance(v, bool):
        raise SaleError("%s must be a whole number of atoms" % name)
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    if isinstance(v, float) and v.is_integer():
        return int(v)
    raise SaleError("%s must be a whole number of atoms" % name)


class BuyPlan:
    """Exactly what the buyer's transaction must contain.

    Levo signs nothing. A plan is the set of constraints the covenant will
    check; `tx.build_buy` turns it into an unsigned transaction the buyer's
    wallet completes, and the plan itself is returned so a buyer can verify,
    before signing, that the outputs are the ones the published terms imply.
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
                "why": "the tokens you bought; put them at any output the "
                       "covenant does not check (index 2 or later)",
            },
        }
