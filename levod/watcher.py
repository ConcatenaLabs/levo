"""The watcher: the chain decides what a sale is, not what anybody claimed.

Levo shows sales, and a sale's state is a fact about the UTXO set: is the
covenant still holding tokens, how many, and at which outpoint. The watcher
answers that by scanning for unspent outputs at each sale's address, which
means:

  * A buy made WITHOUT Levo still moves the sale. The sell leaf is
    permissionless, so purchases can and will happen that Levo never planned. A
    launchpad that only knew about its own purchases would show stale numbers
    forever; this one reads the chain.

  * A partial buy is picked up automatically. The remainder re-rests at the
    identical address, so the scan simply finds a smaller output and the sale
    carries on from the new outpoint.

  * A reorged lock is caught. Sequentia follows its Bitcoin anchor, so a funding
    transaction can be UN-MADE after a sale was already open. When the covenant
    output is gone and the block it was mined into is no longer in the chain,
    the sale is not sold out -- it was never funded. That is GHOST, and it stops
    being investable rather than quietly showing as complete.

Three states look identical from the address alone and mean different things:
a sale that sold out, a sale whose funding was reorged away, and a sale that
closed and was then emptied by its project's reclaim. They are told apart by
evidence the watcher keeps as it goes: the block each resting output was mined
into, and the transaction ids a reclaim or a purchase was built with.

The watcher looks in four places, cheapest and most certain first: the outpoint
it already knows (which `gettxout` answers with the mempool included), any
outpoint a recorded purchase said the remainder would rest at, the confirmed
UTXO set, and finally the mempool itself for a transaction spending the known
outpoint. Only after two consecutive polls find nothing anywhere does it call a
sale finished, and only the block record decides how.
"""

import threading
import time

import sale as S

# How many mempool transactions the spend search will read before giving up.
# Sequentia's mempool is small and a buy confirms within a block or two, so the
# search is a convenience for the minute a remainder is unconfirmed, not a
# thing to spend the node's time on when the pool is deep.
MEMPOOL_SEARCH_LIMIT = 250


class Watcher:
    def __init__(self, market, rpc, interval=60, hrp="tb", log=None):
        self.market = market
        self.rpc = rpc
        self.interval = interval
        self.hrp = hrp
        self.log = log or (lambda m: None)
        self._misses = {}
        self._miss_height = {}
        self.confirm_misses = 2
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.last_run = None
        self.last_error = None

    # --- lifecycle ---------------------------------------------------------

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="levo-watcher")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def nudge(self):
        """Ask for a poll now rather than at the next interval.

        A purchase or a lock the platform just learned about is worth a look
        straight away: the buyer is watching the page, and the chain already
        has the answer.
        """
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:                      # never let the loop die
                self.last_error = str(e)
                self.log("watcher: %s" % e)
            self._wake.wait(self.interval)
            self._wake.clear()

    # --- the work ----------------------------------------------------------

    def poll(self):
        """Reconcile every funded sale against the UTXO set."""
        lock = getattr(self.market, "lock", None)
        if lock is None:
            return self._poll()
        with lock:
            return self._poll()

    def _poll(self):
        sales = [(slug, p) for slug, p in self.market.projects.items()
                 if p.sale and p.sale.status in (S.LIVE, S.PARTIAL, S.SOLD_OUT,
                                                 S.CLOSED, S.GHOST)]
        if not sales:
            self.last_run = time.time()
            return {"checked": 0, "changed": []}

        changed = []
        errors = []
        # The outpoint each sale is known to rest at is checked first, with
        # the mempool included. In the steady state that answers for every
        # sale and the UTXO set is not walked at all.
        pending = []
        for slug, p in sales:
            try:
                settled = self._check_known(p)
            except Exception as e:
                errors.append("%s: %s" % (slug, e))
                continue
            if settled is None:
                pending.append((slug, p))
            elif settled:
                changed.append(slug)

        found = {}
        if pending:
            # One scan covers every sale that needs one: scantxoutset walks
            # the whole UTXO set, so asking about ten addresses costs what
            # asking about one does. A scan that fails is 'unknown', never
            # 'nothing there'.
            try:
                found = self._scan(["raw(%s)" % p.sale.script_pubkey for _, p in pending])
            except Exception as e:
                errors.append("scan: %s" % e)
                found = None
        for slug, p in pending:
            if found is None:
                continue
            try:
                if self._reconcile(p, found):
                    changed.append(slug)
            except Exception as e:
                errors.append("%s: %s" % (slug, e))
        if changed:
            self.market.save()
        self.last_run = time.time()
        self.last_error = "; ".join(errors) if errors else None
        if errors:
            self.log("watcher: " + self.last_error)
        return {"checked": len(sales), "changed": changed, "scanned": bool(pending)}

    def _scan(self, descriptors):
        """{scriptPubKey hex: [{txid, vout, atoms, asset, height}]} for the
        given addresses. Confirmed outputs only: that is all the scan sees."""
        try:
            res = self.rpc.call("scantxoutset", "start", descriptors)
        except Exception as e:
            if "already in progress" in str(e).lower():
                # A scan from a poll that timed out is still running. Abort it
                # so the next poll can start its own; this one reports unknown.
                try:
                    self.rpc.call("scantxoutset", "abort")
                except Exception:
                    pass
            raise
        if not res or not res.get("success"):
            raise RuntimeError("scantxoutset did not complete")
        out = {}
        for u in (res or {}).get("unspents", []) or []:
            spk = (u.get("scriptPubKey") or "").lower()
            out.setdefault(spk, []).append({
                "txid": u.get("txid"),
                "vout": int(u.get("vout", 0)),
                "atoms": _atoms(u),
                "asset": (u.get("asset") or "").lower(),
                "height": u.get("height"),
            })
        return out

    def _check_known(self, project):
        """Ask about the outpoint the sale is known to rest at, with the
        mempool included. Returns True/False (changed or not) when the sale is
        still there, and None when it is not, so the caller looks further.

        This comes before any scan on purpose. `scantxoutset` walks the
        CONFIRMED UTXO set only, so a sale funded a moment ago is invisible to
        it -- and concluding from that silence that the sale is over would take
        a freshly locked sale off the board within a minute of it opening. That
        happened on the live deployment the first time a sale was listed.
        """
        sale = project.sale
        slug = getattr(project, "slug", sale.project_id)
        if not sale.funding:
            return None
        out = self.rpc.txout(sale.funding["txid"], sale.funding["vout"])
        if out is None:
            return None
        self._remember_block(sale, out)
        self._misses.pop(slug, None)
        self._miss_height.pop(slug, None)
        return self._rest(sale, sale.funding["txid"], sale.funding["vout"],
                          _out_atoms(out))

    def _reconcile(self, project, found):
        sale = project.sale
        spk = sale.script_pubkey.lower()
        slug = getattr(project, "slug", sale.project_id)

        # A purchase recorded through Levo names the transaction it was made
        # with, and a partial buy re-rests the remainder at that transaction's
        # output 1. Asking `gettxout` about it sees the mempool, so the sale
        # moves to its new outpoint the moment the buy is broadcast rather than
        # a block or two later.
        for cand in list(sale.candidates):
            try:
                out = self.rpc.txout(cand["txid"], cand["vout"])
            except Exception:
                out = None
            if out is None:
                continue
            if not self._is_resting(out, sale):
                continue
            sale.candidates = []
            self._misses.pop(slug, None)
            adopted = self._rest(sale, cand["txid"], cand["vout"], _out_atoms(out))
            self._remember_block(sale, out)
            return adopted

        at_address = found.get(spk, [])
        resting = [u for u in at_address if u["asset"] == sale.terms.token_asset]
        # Anything else resting at the sale address is not for sale, but the
        # sell leaf does not check what asset it spends, so it can be taken by
        # anyone at the sale's price. The project needs to know.
        strays = [{"txid": u["txid"], "vout": u["vout"], "asset": u["asset"], "atoms": u["atoms"]}
                  for u in at_address if u["asset"] != sale.terms.token_asset]
        if strays != sale.strays:
            sale.strays = strays

        if resting:
            # Prefer the largest: a sale should only ever have one output
            # resting, but a stray payment to the address must not shrink it.
            u = max(resting, key=lambda x: x["atoms"])
            self._misses.pop(slug, None)
            self._miss_height.pop(slug, None)
            changed = self._rest(sale, u["txid"], u["vout"], u["atoms"])
            # The scan reports confirmed outputs and the height each was mined
            # at, so the block can be noted here too. Without it a remainder
            # found by the scan would look like something never seen confirmed,
            # and a sale that then sold out would be reported as a reorg.
            self._remember_height(sale, u.get("height"))
            return changed

        # Nothing resting anywhere we can see so far. The known outpoint may
        # have been spent by a buy whose remainder is still in the mempool,
        # where neither the scan nor a guess can find it -- but the mempool
        # itself can be read.
        if sale.funding and sale.status != S.DRAFT:
            spender = self._mempool_spender(sale.funding["txid"], sale.funding["vout"])
            if spender is not None:
                txid, vouts = spender
                rem = self._remainder_in(vouts, sale)
                if rem is not None:
                    self._misses.pop(slug, None)
                    self._miss_height.pop(slug, None)
                    return self._rest(sale, txid, rem[0], rem[1])
                # The spend leaves nothing behind. Wait for the blocks to say
                # so; a mempool transaction can still be dropped.

        # Before concluding that, wait for a second look: a buy's remainder
        # sits in the mempool for a block or two, and the confirmed-set scan
        # cannot see it either. Declaring a sale finished on one blind reading
        # would flap it in and out of existence.
        if sale.status == S.DRAFT:
            return False
        misses = self._misses.get(slug, 0) + 1
        self._misses[slug] = misses
        height = self._height()
        first = self._miss_height.setdefault(slug, height)
        if misses < self.confirm_misses:
            return False
        # Two silent polls on the same block prove nothing: a remainder in the
        # mempool stays invisible until a block carries it. Wait for the chain
        # to move as well, so a slow block cannot end a sale by itself.
        if height is not None and first is not None and height <= first:
            return False
        return self._finish(sale)

    # --- how a sale ends ---------------------------------------------------

    def _finish(self, sale):
        """Nothing rests, twice in a row. Decide what that means.

        The block the funding was mined into is the evidence that separates
        the three ways a covenant can be empty. If that block is gone, the
        funding went with it (GHOST). If it is intact and the sale had closed,
        the tokens left after the close: a proven reclaim says RECLAIMED, and
        otherwise the sale is CLOSED with nothing left in it. If it is intact
        and the sale had not closed, only a buy could have emptied it, so it
        SOLD OUT.
        """
        was = (sale.status, sale.locked_atoms)
        if not sale.funding:
            # A ghost has no outpoint to reason from. Nothing changes until a
            # re-funding shows up at the address; silence is not a sale.
            return False
        if sale.status == S.SOLD_OUT:
            return False               # sold out is final, whatever the clock does
        if sale.status == S.CLOSED and sale.locked_atoms == 0:
            # Already closed and empty; the only news would be a reclaim
            # Levo built showing up on chain.
            if self._reclaim_landed(sale):
                sale.mark_emptied(reclaimed=True)
            return was != (sale.status, sale.locked_atoms)
        reorged = self._was_reorged(sale)
        if reorged is True:
            sale.mark_ghost()
        elif reorged is None and sale.funding and not sale.funding.get("block"):
            # Never observed confirmed, and now not there at all. It was
            # probably never mined; treat it as unfunded rather than sold.
            sale.mark_ghost()
        else:
            height = self._height()
            if sale.has_closed(height=height):
                sale.mark_emptied(reclaimed=self._reclaim_landed(sale))
            else:
                sale.mark_sold_out()
        return was != (sale.status, sale.locked_atoms)

    def _reclaim_landed(self, sale):
        """True when a reclaim Levo built has visibly delivered the tokens.

        A reclaim pays the swept tokens to its destination at output 0, so if
        that output exists and carries the sale token, the reclaim is on chain.
        A destination that has since been spent cannot be told from a late
        buy, and the sale is then simply closed and empty, which is true too.
        """
        for txid in list(sale.reclaim_txids):
            try:
                out = self.rpc.txout(txid, 0)
            except Exception:
                continue
            if out is None:
                continue
            asset = (out.get("asset") or "").lower()
            if asset == sale.terms.token_asset or not asset:
                return True
        return False

    # --- the pieces --------------------------------------------------------

    def _rest(self, sale, txid, vout, atoms):
        """The covenant rests at this outpoint with this many tokens."""
        before = (sale.status, sale.locked_atoms,
                  sale.funding and sale.funding.get("txid"))
        prior = sale.funding if sale.funding and sale.funding.get("txid") == txid \
            and sale.funding.get("vout") == vout else {}
        sale.funding = dict(prior, txid=txid, vout=int(vout), atoms=atoms)
        sale.locked_atoms = atoms
        total = sale.terms.total_atoms or atoms
        sale.sold_atoms = max(0, total - atoms)
        sale.status = S.LIVE if atoms >= total else S.PARTIAL
        return before != (sale.status, sale.locked_atoms, txid)

    def _is_resting(self, out, sale):
        """Whether a `gettxout` result is this sale's token at this sale's
        address, unblinded."""
        spk = ((out.get("scriptPubKey") or {}).get("hex") or "").lower()
        if spk and spk != sale.script_pubkey.lower():
            return False
        asset = (out.get("asset") or "").lower()
        if asset and asset != sale.terms.token_asset:
            return False
        if out.get("valuecommitment") or out.get("amountcommitment") \
                or out.get("assetcommitment"):
            return False
        return True

    def _remainder_in(self, vouts, sale):
        """(vout, atoms) of the sale token re-rested at the sale address in a
        decoded transaction's outputs, or None."""
        best = None
        for o in vouts or []:
            spk = ((o.get("scriptPubKey") or {}).get("hex") or "").lower()
            if spk != sale.script_pubkey.lower():
                continue
            if (o.get("asset") or "").lower() != sale.terms.token_asset:
                continue
            if o.get("value") is None:
                continue                              # blinded: not a remainder
            atoms = _out_atoms(o)
            if best is None or atoms > best[1]:
                best = (int(o.get("n", 0)), atoms)
        return best

    def _mempool_spender(self, txid, vout):
        """(spending txid, its decoded outputs) for a mempool transaction that
        spends this outpoint, or None. Bounded; a miss just means 'not seen'."""
        try:
            pool = self.rpc.call("getrawmempool") or []
        except Exception:
            return None
        for cand in list(pool)[:MEMPOOL_SEARCH_LIMIT]:
            try:
                raw = self.rpc.call("getrawtransaction", cand, True)
            except Exception:
                continue
            if not raw:
                continue
            for vin in raw.get("vin") or []:
                if vin.get("txid") == txid and int(vin.get("vout", -1)) == int(vout):
                    return raw.get("txid") or cand, raw.get("vout") or []
        return None

    def _height(self):
        try:
            return self.rpc.chain_height()
        except Exception:
            return None

    def _remember_block(self, sale, out):
        """Note which block the sale's funding is in, so a reorg is detectable.

        `gettxout` reports how deep an output is, not where it sits, so the
        height is derived from the depth and the block hash at that height is
        recorded. It is the only thing that later distinguishes a sale that sold
        out from one whose funding was undone.
        """
        try:
            conf = int(out.get("confirmations") or 0)
            if conf < 1:
                return
            # `gettxout` names the tip it answered against, so the height is
            # taken from that block rather than from a later, possibly moved,
            # tip. A node that does not name it is asked for the tip instead.
            tip = None
            best = out.get("bestblock")
            if best:
                try:
                    tip = int((self.rpc.call("getblockheader", best) or {}).get("height"))
                except Exception:
                    tip = None
            if tip is None:
                tip = self.rpc.chain_height()
            self._remember_height(sale, tip - conf + 1)
        except Exception:
            pass                       # best effort; absence just means we ask later

    def _remember_height(self, sale, height):
        """Record the block at this height as the funding's block. Always
        re-read, never trust the note: a one-block reorg replaces the block at
        the same height and usually re-includes the funding, and the stale
        hash would later read a sold-out sale as a ghost."""
        if height is None or not sale.funding:
            return
        try:
            height = int(height)
            block = self.rpc.call("getblockhash", height)
            if not block:
                return
            sale.funding["height"] = height
            sale.funding["block"] = block
        except Exception:
            pass

    def _was_reorged(self, sale):
        """Positive evidence that the funding was undone, or None if unknown.

        This is deliberately not 'we could not find the transaction'. A node
        without -txindex cannot find a fully spent transaction either, so
        treating absence as proof reports a sold-out sale as a reorg -- which
        tells buyers it was never funded, the opposite of the truth.

        The block the funding was mined into is the evidence. If the chain no
        longer has that block at that height, the funding is gone with it;
        Sequentia follows its Bitcoin anchor, so that genuinely happens.
        """
        f = sale.funding or {}
        height, block = f.get("height"), f.get("block")
        if not block or height is None:
            return None                # never seen it confirmed: cannot say
        tip = self._height()
        if tip is not None and int(height) > tip:
            return True                # the chain no longer reaches that height
        try:
            return self.rpc.call("getblockhash", height) != block
        except Exception:
            return None


def _out_atoms(out):
    """Atoms held by a `gettxout` result or a decoded output."""
    if out.get("valueatoms") is not None:
        return int(out["valueatoms"])
    if out.get("amountatoms") is not None:
        return int(out["amountatoms"])
    return int(round(float(out.get("value", 0)) * 100_000_000))


def _atoms(u):
    if u.get("amountatoms") is not None:
        return int(u["amountatoms"])
    if u.get("valueatoms") is not None:
        return int(u["valueatoms"])
    return int(round(float(u.get("amount", 0)) * 100_000_000))
