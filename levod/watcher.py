"""The watcher: the chain decides what a sale is, not what anybody claimed.

Levo shows sales, and a sale's state is a fact about the UTXO set: is the
covenant still holding tokens, how many, and at which outpoint. The watcher
answers that against the node, which means:

  * A buy made WITHOUT Levo still moves the sale. The sell leaf is
    permissionless, so purchases can and will happen that Levo never planned. A
    launchpad that only knew about its own purchases would show stale numbers
    forever; this one reads the chain.

  * A partial buy is picked up automatically. The remainder re-rests at the
    identical address, so the watcher finds a smaller output at a new outpoint
    and the sale carries on from there.

  * A reorged lock is caught. Sequentia follows its Bitcoin anchor, so a funding
    transaction can be UN-MADE after a sale was already open. When that happens
    the sale is not sold out -- it was never funded. That is GHOST, and it stops
    being investable rather than quietly showing as complete.

Three states look identical from the address alone and mean different things: a
sale that sold out, a sale whose funding was reorged away, and a sale the
project reclaimed after its close. Telling them apart is this module's whole
job, and it is done on POSITIVE EVIDENCE, never on silence.

The evidence is the chain's own record of the covenant's outputs. Every resting
output the watcher adopts is dated: the block it confirmed in, or, while it is
still in the mempool, the height at which it was first seen. When an outpoint
is replaced by its successor the older one's block is carried forward as the
sale's ancestor, so a chain of spends that began in a block still on the chain
proves the sale is real however fast the spends came. A sale is called a ghost
only when the chain says its funding is gone: the block it was mined in is no
longer at that height, or it was never mined and is in no block and no mempool.
Anything else -- a node still syncing, a chain that shrank, an RPC that failed,
evidence not yet gathered -- leaves the sale exactly as it was.

The watcher looks in four places, cheapest and most certain first: the outpoint
it already knows (which `gettxout` answers with the mempool included), any
outpoint a recorded purchase said the remainder would rest at, the mempool
itself for a transaction spending the known outpoint, and the confirmed UTXO
set. It ends a sale only after two consecutive polls with a new block between
them find nothing anywhere, because a remainder in the mempool is invisible to
the confirmed-set scan until a block carries it.
"""

import threading
import time
from contextlib import contextmanager

import rpc as RPCMOD
import sale as S
import tx as TX

# How many mempool transactions the spend search will read before giving up.
# Sequentia's mempool is small and a buy confirms within a block or two, so the
# search is a convenience for the minute a remainder is unconfirmed, not a
# thing to spend the node's time on when the pool is deep.
MEMPOOL_SEARCH_LIMIT = 250

# Polls to wait before walking the chain again after a walk that failed part
# way. Two hundred blocks a minute against a node that is already struggling
# helps nobody.
SEARCH_RETRY_POLLS = 5

# How far back the watcher will walk blocks looking for a funding transaction
# it never saw confirmed. Beyond this it says it does not know, rather than
# guessing.
BLOCK_SEARCH_LIMIT = 200

# How often a resting sale's address is scanned for assets that are not the
# sale's token. They cannot be sold by the covenant's own terms but can be
# taken by anyone at the sale's price, so the project is told; a stray is not
# urgent enough to walk the UTXO set every minute.
STRAY_SCAN_EVERY = 10

# How many foreign outputs a sale reports at its address. Anyone can pay dust
# to one, and a hundred lines of dust say no more than twenty do.
MAX_STRAYS = 20


class Watcher:
    def __init__(self, market, rpc, interval=60, hrp="tb", log=None, note=None):
        self.market = market
        self.rpc = rpc
        self.interval = interval
        self._hrp = hrp
        self.log = log or (lambda m: None)
        # Two channels on purpose. `log` is for what went wrong; `note` is for
        # what happened -- a sale changing state is the record an operator
        # needs at three in the morning when a project asks why its sale reads
        # as it does, and it is not an error.
        self.note = note or self.log
        self._misses = {}
        self._miss_height = {}
        self._search_after = {}       # sale -> the poll a failed block walk may retry at
        self._blocks = {}             # block hashes and heights, for one poll
        self._round = 0
        self.confirm_misses = 2
        # Sales whose funding this levod cannot place in the chain: state
        # restored from a backup taken before locks were dated. They are left
        # exactly as they were, and named here so an operator can see why.
        self.unverified = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.last_run = None
        self.last_error = None
        # Polls that have failed since the last clean one. A single failure is
        # a bad minute on the node; a run of them means nothing is being
        # reconciled, which is what a monitor needs to know.
        self.consecutive_errors = 0

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
                self.consecutive_errors += 1
                self.log("watcher: %s" % e)
            self._wake.wait(self.interval)
            self._wake.clear()

    # --- the work ----------------------------------------------------------

    def poll(self):
        """Reconcile every funded sale against the chain.

        The chain is read outside the platform's lock, which is then taken only
        to write what was learned: a UTXO-set scan can take minutes, and a
        buyer's purchase must not wait behind it.
        """
        self._round += 1
        # Sales confirmed in the same block ask for the same hash, and a poll
        # over a thousand of them asked a thousand times. The memo lives for
        # one poll, so a reorg between polls is still seen.
        self._blocks = {}
        sales = self._sales()
        if not sales:
            self.last_run = time.time()
            self.last_error = None
            return {"checked": 0, "changed": [], "scanned": False}

        chain = self._chain_state()
        errors = []
        changed = []
        dirty = False
        pending = []

        # The outpoint each sale is known to rest at, with the mempool
        # included. In the steady state that answers for every sale and the
        # UTXO set is not walked at all.
        for slug, p in sales:
            was = _shape(p.sale)
            try:
                settled, note = self._check_known(p, chain)
            except Exception as e:
                errors.append("%s: %s" % (slug, e))
                continue
            self._announce(slug, p.sale, was)
            dirty = dirty or note
            if settled is None:
                pending.append((slug, p))
            elif settled:
                changed.append(slug)

        # Listings nobody has asked the registry about yet: a few a poll, so a
        # deployment that gains a registry answers for the sales it already
        # had, without a burst.
        try:
            if getattr(self.market, "check_registry", None) and self.market.check_registry():
                dirty = True
        except Exception as e:
            errors.append("registry: %s" % e)

        # A scan is needed for sales whose outpoint is gone, and occasionally
        # for the rest, to notice assets resting at their address that the
        # covenant does not sell.
        stray_round = (self._round % STRAY_SCAN_EVERY) == 0
        scan_for = list(pending)
        if stray_round:
            known = {slug for slug, _ in pending}
            scan_for += [(slug, p) for slug, p in sales
                         if slug not in known and p.sale.funding]
        found = {}
        scanned = False
        if scan_for:
            try:
                found = self._scan(["raw(%s)" % p.sale.script_pubkey for _, p in scan_for])
                scanned = True
            except Exception as e:
                errors.append("scan: %s" % e)
                found = None

        if found is not None:
            for slug, p in scan_for:
                try:
                    note = self._note_strays(p, found)
                    dirty = dirty or note
                except Exception as e:
                    errors.append("%s strays: %s" % (slug, e))
            for slug, p in pending:
                was = _shape(p.sale)
                try:
                    if self._reconcile(p, found, chain):
                        changed.append(slug)
                    self._announce(slug, p.sale, was)
                except Exception as e:
                    errors.append("%s: %s" % (slug, e))

        # A save that failed leaves the platform's state ahead of the disk;
        # trying again every poll is the only thing that recovers a full disk
        # once it has been emptied, and it is free when nothing is wrong.
        store = getattr(self.market, "store", None)
        if changed or dirty or (store is not None and store.dirty):
            try:
                self.market.save()
            except Exception as e:
                errors.append("saving the state file: %s" % e)
        self.unverified = sorted(
            slug for slug, p in sales
            if (p.sale.funding or {}).get("unverifiable"))
        self.last_run = time.time()
        self.last_error = "; ".join(errors) if errors else None
        self.consecutive_errors = self.consecutive_errors + 1 if errors else 0
        if errors:
            self.log("watcher: " + self.last_error)
        return {"checked": len(sales), "changed": changed, "scanned": scanned,
                "unverified": list(self.unverified)}

    @property
    def hrp(self):
        return self._hrp() if callable(self._hrp) else self._hrp

    def _announce(self, slug, sale, was):
        """Say what changed, and on what evidence.

        A sale's state is the platform's most consequential output, and until
        this line existed there was no record anywhere of when one changed or
        why -- only the answer it happens to give now.
        """
        now = _shape(sale)
        if now == was:
            return
        f = sale.funding or {}
        where = ("%s:%s" % (f.get("txid", "")[:12], f.get("vout"))) if f else "nothing"
        at = f.get("height") or f.get("ancestor_height") or f.get("seen_height")
        self.note("%s: %s -> %s, holding %s at %s%s"
                  % (slug, was[0], now[0], now[1], where,
                     (" (block %s)" % at) if at else ""))

    @contextmanager
    def _held(self):
        """The platform's lock, for the moment a sale is written.

        The watcher runs beside the request threads and both write sales, so
        every write here is made under it. It is never held across a chain
        read: a UTXO-set scan takes minutes, a mempool search is hundreds of
        calls, and the watcher's own RPC handle waits five minutes before it
        gives up. Holding the lock across any of that would park every
        purchase, lock and listing behind it -- and the shutdown path takes the
        same lock, so systemd would kill levod rather than stop it.
        """
        lock = getattr(self.market, "lock", None)
        if lock is None:
            yield
        else:
            with lock:
                yield

    def _sales(self):
        """The sales worth looking at, snapshotted under the platform's lock.

        A reclaimed sale is finished: its tokens are back with the project and
        nothing on chain can change that. Everything else is still capable of
        moving, a ghost included -- it comes back if the project locks again.
        """
        lock = getattr(self.market, "lock", None)
        if lock is None:
            return [(slug, p) for slug, p in self.market.projects.items()
                    if p.sale and p.sale.status in (S.LIVE, S.PARTIAL, S.SOLD_OUT,
                                                    S.CLOSED, S.GHOST)]
        with lock:
            return [(slug, p) for slug, p in list(self.market.projects.items())
                    if p.sale and p.sale.status in (S.LIVE, S.PARTIAL, S.SOLD_OUT,
                                                    S.CLOSED, S.GHOST)]

    def _chain_state(self):
        """Height, and whether the node is in a state worth drawing conclusions
        from.

        A node rebuilding its chain reports a tip climbing up from zero, and
        every sale on the platform would ghost against it. A tip that simply
        FELL is a different matter and is not guarded against: Sequentia
        follows its Bitcoin anchor, so a shorter chain is the normal way a
        reorg arrives, and a funding that went with it really is gone.
        """
        try:
            info = self.rpc.chain_info()
        except Exception:
            return {"height": None, "usable": False, "mediantime": None}
        height = int(info.get("blocks") or 0)
        return {"height": height, "mediantime": info.get("mediantime"),
                "usable": not info.get("initialblockdownload")}

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

    # --- the four places to look -------------------------------------------

    def _check_known(self, project, chain):
        """Ask about the outpoint the sale is known to rest at, with the
        mempool included. Returns (settled, noted): `settled` is True/False
        when the sale is still there (changed or not) and None when it is not,
        so the caller looks further; `noted` says whether anything worth saving
        was learned.

        This comes before any scan on purpose. `scantxoutset` walks the
        CONFIRMED UTXO set only, so a sale funded a moment ago is invisible to
        it -- and concluding from that silence that the sale is over would take
        a freshly locked sale off the board within a minute of it opening.
        """
        sale = project.sale
        slug = getattr(project, "slug", sale.project_id)
        if not sale.funding:
            # A ghost has no outpoint. It comes back only by being funded
            # again, which the scan finds.
            return (None, False) if sale.status == S.GHOST else (None, False)
        out = self.rpc.txout(sale.funding["txid"], sale.funding["vout"])
        if out is not None:
            noted = self._remember_block(sale, out, chain)
            self._misses.pop(slug, None)
            self._miss_height.pop(slug, None)
            changed = self._rest(sale, sale.funding["txid"], sale.funding["vout"],
                                 _out_atoms(out))
            return changed, noted
        # The outpoint is spent. For a sale that has already ended, that is the
        # normal case and there is nothing more to look for: the check just
        # made is also what would notice a reorg putting it back.
        if sale.status == S.SOLD_OUT:
            return False, False
        if sale.status == S.CLOSED and sale.locked_atoms == 0:
            if self._reclaim_landed(sale):
                with self._held():
                    sale.mark_emptied(reclaimed=True)
                return True, False
            # Nothing rests here as far as this sale knows, but the address can
            # still receive: a stray, or tokens sent after the close. Let the
            # scan look on its ordinary cadence rather than never again.
            return None if (self._round % STRAY_SCAN_EVERY) == 0 else False, False
        return None, False

    def _reconcile(self, project, found, chain):
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
            if out is None or not self._is_resting(out, sale):
                continue
            # A remainder is what a BUY leaves behind, so the transaction
            # holding it must spend the outpoint this sale was resting at.
            # Anyone can record a purchase naming any transaction, and without
            # this an account could point the watcher at an output of its own
            # -- tokens it sent to the sale address itself -- and make a sale
            # that is nearly full read as nearly sold out.
            if not self._spends_funding(cand["txid"], sale):
                continue
            atoms = _out_atoms(out)
            if 0 < atoms < sale.terms.min_lot:
                # A remainder the covenant refuses to sell is not a remainder.
                # Adopting one would leave the sale resting on an amount every
                # later purchase is refused against.
                continue
            with self._held():
                # Only the one that was adopted. Clearing the list threw away a
                # second buy's hint, which another thread may have added
                # between two of the chain reads above, and the poll's own save
                # then persisted the loss.
                sale.candidates = [c for c in sale.candidates if c != cand]
            self._misses.pop(slug, None)
            self._miss_height.pop(slug, None)
            adopted = self._rest(sale, cand["txid"], cand["vout"], atoms)
            self._remember_block(sale, out, chain)
            return adopted

        # The mempool, for a buy Levo never planned. This has to come before
        # the confirmed-set scan: `gettxout` has just said the known outpoint
        # is spent, and the scan cannot see that yet, so the scan would keep
        # re-adopting an outpoint that is already gone.
        if sale.funding and sale.status != S.DRAFT:
            spender = self._mempool_spender(sale.funding["txid"], sale.funding["vout"])
            if spender is not None:
                txid, vouts = spender
                rem = self._remainder_in(vouts, sale)
                if rem is not None:
                    self._misses.pop(slug, None)
                    self._miss_height.pop(slug, None)
                    adopted = self._rest(sale, txid, rem[0], rem[1])
                    with self._held():
                        if sale.funding is not None \
                                and sale.funding.get("seen_height") is None \
                                and chain.get("height") is not None:
                            sale.funding["seen_height"] = chain["height"]
                    return adopted
                # The spend leaves nothing behind: the sale is being bought
                # out. Wait for the block that carries it rather than deciding
                # from a transaction that can still be dropped.
                return False

        # A remainder the covenant left is always at least the minimum lot: the
        # sell leaf refuses to leave less behind. So a smaller output of the
        # sale token at the sale address is not the sale -- it is something
        # somebody sent there -- and adopting it would leave the sale resting
        # on an amount no purchase can ever take, reading almost sold out for
        # ever. One atom would do it.
        resting = [u for u in found.get(spk, [])
                   if u["asset"] == sale.terms.token_asset
                   and u["atoms"] >= sale.terms.min_lot
                   and not (sale.funding and u["txid"] == sale.funding.get("txid")
                            and u["vout"] == sale.funding.get("vout"))]

        if resting:
            # Prefer the largest: a sale should only ever have one output
            # resting, but a stray payment to the address must not shrink it.
            u = max(resting, key=lambda x: x["atoms"])
            self._misses.pop(slug, None)
            self._miss_height.pop(slug, None)
            changed = self._rest(sale, u["txid"], u["vout"], u["atoms"])
            # The scan reports confirmed outputs and the height each was mined
            # at, so the block can be noted here too. Without it a remainder
            # found by the scan would look like something never seen confirmed.
            if u.get("height") is not None:
                self._remember_height(sale, u["height"])
            else:
                # A node that does not say where it was mined still says that
                # it WAS: the scan reads the confirmed set and nothing else.
                # Recording that much keeps a later silence from reading as a
                # funding that never landed.
                with self._held():
                    if sale.funding is not None:
                        sale.funding["mined"] = True
            return changed

        # Nothing anywhere. Before concluding that, wait for a second look with
        # a new block behind it: a buy's remainder sits in the mempool for a
        # block or two, and the confirmed-set scan cannot see it either.
        if sale.status == S.DRAFT or not sale.funding:
            return False
        misses = self._misses.get(slug, 0) + 1
        self._misses[slug] = misses
        height = chain.get("height")
        # Both clocks as they were at the FIRST miss, not at the poll that
        # decides. The protocol guarantees the deciding poll is at least one
        # block later, and a sale that emptied at its close would be read from
        # that later tip as one that had already closed -- a sold-out sale
        # recorded as closed with nothing sold.
        first_h, first_t = self._miss_height.setdefault(
            slug, (height, chain.get("mediantime")))
        if misses < self.confirm_misses:
            return False
        if height is not None and first_h is not None and height <= first_h:
            return False
        return self._finish(sale, chain, first_h, first_t)

    # --- how a sale ends ---------------------------------------------------

    def _finish(self, sale, chain, first_h=None, first_t=None):
        """Nothing rests, twice in a row, across a block. Decide what that
        means -- but only on evidence.

        The question is whether the covenant's funding is still part of the
        chain. If it is, the tokens left the way the covenant allows: a buy
        before the close (SOLD_OUT), or, after it, a buy or the project's own
        reclaim (CLOSED, or RECLAIMED when the reclaim itself is visible). If
        the funding is gone from the chain, the sale was never funded (GHOST).
        If the chain cannot say, nothing changes.
        """
        was = (sale.status, sale.locked_atoms)
        if not chain.get("usable"):
            return False
        landed = self._funding_landed(sale, chain)
        if landed is None:
            landed = self._purchase_landed(sale)
        if landed is None:
            return False                       # no evidence either way: wait
        if landed is False:
            with self._held():
                sale.mark_ghost()
            return was != (sale.status, sale.locked_atoms)
        # Was the covenant emptied before a reclaim was possible? If so a
        # buyer emptied it, whatever the tip says now.
        if first_h is None:
            first_h = chain.get("height")
        if first_t is None:
            first_t = chain.get("mediantime")
        closed = sale.reclaim_possible_at(height=first_h, now=first_t)
        # A reclaim that is actually on chain settles it either way: the chain
        # would not have accepted one before the close.
        reclaimed = bool(self._reclaim_landed(sale)) \
            if (closed or sale.reclaim_txids) else False
        if reclaimed:
            closed = True
        with self._held():
            if closed:
                sale.mark_emptied(reclaimed=reclaimed)
            else:
                sale.mark_sold_out()
        return was != (sale.status, sale.locked_atoms)

    def _funding_landed(self, sale, chain):
        """True when the chain still carries the block this sale's covenant
        was funded in, False when that block is gone or the funding was never
        mined at all, None when the node cannot say.

        A sale that has moved keeps its ancestor: the block an earlier resting
        output confirmed in. A chain of spends that began in a block still on
        the chain is proof the covenant was real, however quickly the spends
        followed one another.
        """
        f = sale.funding or {}
        for h, b in ((f.get("height"), f.get("block")),
                     (f.get("ancestor_height"), f.get("ancestor_block"))):
            if b and h is not None:
                try:
                    if self._block_at(h) == b:
                        return True
                except Exception:
                    tip = chain.get("height")
                    if tip is not None and int(h) > tip:
                        return False        # the chain no longer reaches that height
                    return None
                # The block that carried it is not the block at that height any
                # more. That is a reorg -- but a reorg usually RE-MINES the same
                # transactions into the new block, and a sale whose funding came
                # back is not a ghost. Ask the chain where the funding is now
                # before concluding it is gone.
                return self._mined_recently(sale, chain, definite=True)
        if f.get("seen_height") is None and not f.get("mined"):
            # Undated: state written before Levo dated its locks, or restored
            # from a backup taken then. The funding may be perfectly real and
            # long since spent. Look for it in the recent chain, and if it is
            # not there, say so rather than guessing -- calling a sold-out sale
            # a ghost wipes every buyer's allocation, and that is a far worse
            # answer than an old sale that sits unverified.
            return self._mined_recently(sale, chain)
        # Seen, but never in a block. Look for the funding transaction in the
        # blocks since; a transaction in no block and no mempool was never
        # made, which is the one case that is genuinely a ghost.
        return self._mined_since(sale, chain)

    def _mined_recently(self, sale, chain, definite=False):
        """Look back over the recent chain for a funding this watcher cannot
        place. True when it is there.

        `definite` says what silence means. After a reorg the funding's block is
        known to be gone, so a transaction that is nowhere in the new chain and
        nowhere in the mempool really is gone: False. Without that, silence only
        means this levod cannot tell, and the answer is None -- an undated sale
        is never ghosted on the platform's own forgetfulness.

        The undated search is asked once per sale: nothing about an old funding
        changes from poll to poll, and it reads a block at a time.
        """
        f = sale.funding or {}
        tip = chain.get("height")
        txid = f.get("txid")
        if not txid or tip is None:
            return None
        if f.get("unverifiable") and not definite:
            return None
        if self._round < self._search_after.get(sale.project_id, 0):
            return None                # a failed walk, waiting before another
        try:
            for h in range(int(tip), max(1, int(tip) - BLOCK_SEARCH_LIMIT) - 1, -1):
                block = self.rpc.call("getblock", self.rpc.call("getblockhash", h), 1) or {}
                if txid in (block.get("tx") or []):
                    self._remember_height(sale, h)
                    return True
        except Exception:
            # The walk failed part way. Trying again on the very next poll
            # would re-read two hundred blocks a minute against a node that is
            # already struggling, so it waits for a few.
            self._search_after[sale.project_id] = self._round + SEARCH_RETRY_POLLS
            return None
        if definite:
            try:
                if txid in (self.rpc.call("getrawmempool") or []):
                    return None            # back in the mempool, waiting again
            except Exception:
                return None
            return False                   # reorged out and not re-mined
        with self._held():
            if sale.funding is f:
                f["unverifiable"] = True
        self.log("watcher: %s: its funding %s:%s is in none of the last %d blocks "
                 "and this levod never saw it. The sale is left exactly as it was; "
                 "it is not called a ghost on a guess."
                 % (sale.project_id, txid, f.get("vout"), BLOCK_SEARCH_LIMIT))
        return None

    def _mined_since(self, sale, chain):
        f = sale.funding or {}
        txid = f.get("txid")
        start = f.get("seen_height")
        tip = chain.get("height")
        if f.get("mined"):
            return None          # mined, but the node did not say where
        if not txid or start is None or tip is None:
            return None
        if tip - int(start) > BLOCK_SEARCH_LIMIT:
            return None                       # too far back to say cheaply
        try:
            for h in range(int(start), tip + 1):
                block = self.rpc.call("getblock", self.rpc.call("getblockhash", h), 1) or {}
                if txid in (block.get("tx") or []):
                    self._remember_height(sale, h)
                    return True
            if txid in (self.rpc.call("getrawmempool") or []):
                return None                   # still waiting to be mined
        except Exception:
            return None
        return False

    def _purchase_landed(self, sale):
        """True when a purchase Levo recorded really spent this sale.

        Paying the treasury is not enough on its own -- anyone may pay a
        treasury, and a recorded purchase is a claim by whoever made it. The
        transaction has to spend the outpoint the sale was resting at, which
        only the covenant's own spend can do. Used only to prove a sale was
        real, never to prove it was not.
        """
        want = TX.treasury_script_pubkey(sale.terms).hex()
        for entries in sale.purchases.values():
            for e in list(entries)[-4:]:
                txid = e.get("txid")
                if not txid or e.get("voided"):
                    continue
                if not self._spends_funding(txid, sale):
                    continue
                try:
                    out = self.rpc.txout(txid, 0)
                except Exception:
                    continue
                if not out:
                    continue
                spk = ((out.get("scriptPubKey") or {}).get("hex") or "").lower()
                asset = (out.get("asset") or "").lower()
                if spk == want and asset in ("", sale.terms.payment_asset):
                    return True
        return None

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
        with self._held():
            return self._rest_locked(sale, txid, vout, atoms)

    def _rest_locked(self, sale, txid, vout, atoms):
        before = (sale.status, sale.locked_atoms,
                  sale.funding and sale.funding.get("txid"),
                  sale.funding and sale.funding.get("vout"))
        old = sale.funding or {}
        same = old.get("txid") == txid and old.get("vout") == int(vout)
        if same:
            keep = dict(old)
        else:
            # A new outpoint: carry the old one's block forward as the sale's
            # ancestor, so the lineage back to a block still on the chain is
            # never lost, however many times the covenant moves.
            keep = {}
            anc_h, anc_b = old.get("height"), old.get("block")
            if anc_b is None:
                anc_h, anc_b = old.get("ancestor_height"), old.get("ancestor_block")
            if anc_b is not None and anc_h is not None:
                keep = {"ancestor_height": anc_h, "ancestor_block": anc_b}
            elif old.get("mined"):
                keep = {"mined": True}
        keep.pop("unverifiable", None)      # it is right here; nothing is unverified
        keep.update({"txid": txid, "vout": int(vout), "atoms": atoms})
        sale.funding = keep
        sale.locked_atoms = atoms
        total = sale.terms.total_atoms or atoms
        sale.sold_atoms = max(0, total - atoms)
        sale.status = S.LIVE if atoms >= total else S.PARTIAL
        return before != (sale.status, sale.locked_atoms, txid, int(vout))

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
            if o.get("value") is None and o.get("valueatoms") is None:
                continue                              # blinded: not a remainder
            atoms = _out_atoms(o)
            if best is None or atoms > best[1]:
                best = (int(o.get("n", 0)), atoms)
        return best

    def _spends_funding(self, txid, sale):
        """Whether this transaction spends the outpoint the sale rests at.

        Unknown counts as no: the check exists to stop an outpoint nobody can
        connect to the sale from being adopted, and a node that cannot answer
        has connected nothing. The scan and the mempool search find a real
        remainder anyway, a block later.
        """
        f = sale.funding or {}
        if not f.get("txid"):
            return False
        try:
            raw = self.rpc.call("getrawtransaction", txid, True) or {}
        except Exception:
            return False
        for vin in raw.get("vin") or []:
            if vin.get("txid") == f["txid"] and int(vin.get("vout", -1)) == int(f["vout"]):
                return True
        return False

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

    def _note_strays(self, project, found):
        """Record assets resting at a sale's address that are not its token.

        The sell leaf reads the value of the input it spends, not its asset, so
        anything else that lands at the address can be taken by anyone at the
        sale's price. The project is told so it can sweep it after the close.
        """
        sale = project.sale
        at_address = found.get(sale.script_pubkey.lower(), [])
        # Anyone can pay dust to a sale address, so the report is capped: it
        # exists to tell a project that something is there, and a hundred lines
        # of dust say that no better than twenty do. The largest are kept,
        # because those are the ones worth sweeping.
        # Anything at the address the covenant cannot sell: another asset, or
        # an amount of the sale token below the minimum lot, which the sell
        # leaf would refuse to leave behind and so can only have been sent.
        others = [u for u in at_address
                  if u["asset"] != sale.terms.token_asset
                  or (u["atoms"] < sale.terms.min_lot
                      and not (sale.funding and u["txid"] == sale.funding.get("txid")
                               and u["vout"] == sale.funding.get("vout")))]
        others.sort(key=lambda u: -u["atoms"])
        strays = [{"txid": u["txid"], "vout": u["vout"], "asset": u["asset"],
                   "atoms": u["atoms"]}
                  for u in others[:MAX_STRAYS]]
        if len(others) > MAX_STRAYS:
            self.log("watcher: %s has %d foreign outputs at its address; the "
                     "%d largest are reported"
                     % (sale.project_id, len(others), MAX_STRAYS))
        if strays != sale.strays:
            with self._held():
                sale.strays = strays
            return True
        return False

    def _remember_block(self, sale, out, chain):
        """Note which block the sale's current output is in, so a reorg is
        detectable, or the height at which it was first seen unconfirmed, so a
        funding that never lands can be told from one that was spent.

        `gettxout` reports how deep an output is, not where it sits, so the
        height is derived from the depth against the tip that answer was made
        against.
        """
        try:
            conf = int(out.get("confirmations") or 0)
        except (TypeError, ValueError):
            conf = 0
        if conf < 1:
            with self._held():
                if sale.funding is not None and sale.funding.get("seen_height") is None \
                        and chain.get("height") is not None:
                    sale.funding["seen_height"] = chain["height"]
                    return True
            return False
        try:
            # `gettxout` names the tip it answered against, so the height is
            # taken from that block rather than from a later, possibly moved,
            # tip. A node that does not name it is asked for the tip instead.
            tip = None
            best = out.get("bestblock")
            if best:
                try:
                    tip = self._header_height(best)
                except Exception:
                    tip = None
            if tip is None:
                tip = chain.get("height")
            if tip is None:
                return False
            return self._remember_height(sale, tip - conf + 1)
        except Exception:
            return False               # best effort; absence just means we ask later

    def _block_at(self, height):
        """The hash at a height, asked for once per poll."""
        height = int(height)
        if height not in self._blocks:
            self._blocks[height] = self.rpc.call("getblockhash", height)
        return self._blocks[height]

    def _header_height(self, block_hash):
        """The height of a block, asked for once per poll. The tip is the same
        block for every sale in a poll, so this is one call, not one a sale."""
        key = ("h", block_hash)
        if key not in self._blocks:
            self._blocks[key] = int(
                (self.rpc.call("getblockheader", block_hash) or {}).get("height"))
        return self._blocks[key]

    def _remember_height(self, sale, height):
        """Record the block at this height as this output's block, and say
        whether anything changed. Always re-read the hash, never trust the
        note: a one-block reorg replaces the block at the same height and
        usually re-includes the transaction, and a stale hash would later read
        a sold-out sale as a ghost."""
        if height is None or not sale.funding:
            return False
        try:
            height = int(height)
            block = self._block_at(height)
            if not block:
                return False
            with self._held():
                if not sale.funding:
                    return False
                if sale.funding.get("height") == height \
                        and sale.funding.get("block") == block:
                    return False
                sale.funding["height"] = height
                sale.funding["block"] = block
                sale.funding.pop("seen_height", None)
                sale.funding.pop("unverifiable", None)
            return True
        except Exception:
            return False


def _shape(sale):
    """What a change is measured against: the state and what it holds."""
    return (sale.status, sale.locked_atoms)


def _out_atoms(out):
    """Atoms held by a `gettxout` result or a decoded output, exactly."""
    if out.get("valueatoms") is not None:
        return int(out["valueatoms"])
    if out.get("amountatoms") is not None:
        return int(out["amountatoms"])
    v = out.get("value")
    if v is None:
        v = out.get("amount")
    return RPCMOD.to_atoms(v) if v is not None else 0


def _atoms(u):
    if u.get("amountatoms") is not None:
        return int(u["amountatoms"])
    if u.get("valueatoms") is not None:
        return int(u["valueatoms"])
    v = u.get("amount")
    if v is None:
        v = u.get("value")
    return RPCMOD.to_atoms(v) if v is not None else 0
