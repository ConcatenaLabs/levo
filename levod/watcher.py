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
    output is gone and the funding transaction has vanished from the chain
    entirely, the sale is not sold out -- it was never funded. That is GHOST,
    and it stops being investable rather than quietly showing as complete.

The distinction in that last point is the whole reason the watcher looks at the
funding transaction as well as the address: a sale that sold out and a sale
whose funding was reorged both have nothing resting at the address, and they
mean opposite things.
"""

import threading
import time

import sale as S


class Watcher:
    def __init__(self, market, rpc, interval=60, hrp="tb", log=None):
        self.market = market
        self.rpc = rpc
        self.interval = interval
        self.hrp = hrp
        self.log = log or (lambda m: None)
        self._misses = {}
        self.confirm_misses = 2
        self._stop = threading.Event()
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

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:                      # never let the loop die
                self.last_error = str(e)
                self.log("watcher: %s" % e)
            self._stop.wait(self.interval)

    # --- the work ----------------------------------------------------------

    def poll(self):
        """Reconcile every funded sale against the UTXO set."""
        sales = [(slug, p) for slug, p in self.market.projects.items()
                 if p.sale and p.sale.status in (S.LIVE, S.PARTIAL, S.SOLD_OUT,
                                                 S.CLOSED, S.GHOST)]
        if not sales:
            self.last_run = time.time()
            return {"checked": 0, "changed": []}

        # One scan covers every sale: scantxoutset walks the whole UTXO set, so
        # asking about ten addresses at once costs what asking about one does.
        descriptors = ["raw(%s)" % p.sale.script_pubkey for _, p in sales]
        found = self._scan(descriptors)

        changed = []
        for slug, p in sales:
            try:
                if self._reconcile(p, found):
                    changed.append(slug)
            except Exception as e:
                self.log("watcher %s: %s" % (slug, e))
        if changed:
            self.market.save()
        self.last_run = time.time()
        self.last_error = None
        return {"checked": len(sales), "changed": changed}

    def _scan(self, descriptors):
        """{scriptPubKey hex: [(txid, vout, atoms, asset)]} for the given
        addresses."""
        res = self.rpc.call("scantxoutset", "start", descriptors)
        out = {}
        for u in (res or {}).get("unspents", []) or []:
            spk = (u.get("scriptPubKey") or "").lower()
            out.setdefault(spk, []).append({
                "txid": u.get("txid"),
                "vout": int(u.get("vout", 0)),
                "atoms": _atoms(u),
                "asset": (u.get("asset") or "").lower(),
            })
        return out

    def _reconcile(self, project, found):
        sale = project.sale
        spk = sale.script_pubkey.lower()

        # Ask about the outpoint we already know FIRST, and with the mempool
        # included. `scantxoutset` walks the CONFIRMED UTXO set only, so a sale
        # funded a moment ago is invisible to it -- and concluding from that
        # silence that the sale is over would take a freshly locked sale off the
        # board within a minute of it opening. That happened on the live
        # deployment the first time a sale was listed.
        if sale.funding:
            try:
                out = self.rpc.txout(sale.funding["txid"], sale.funding["vout"])
            except Exception:
                return False                      # cannot tell: change nothing
            if out is not None:
                atoms = _out_atoms(out)
                before = (sale.status, sale.locked_atoms)
                total = sale.terms.total_atoms or atoms
                sale.locked_atoms = atoms
                sale.funding["atoms"] = atoms
                sale.sold_atoms = max(0, total - atoms)
                sale.status = S.LIVE if atoms >= total else S.PARTIAL
                self._misses.pop(project.slug, None)
                return before != (sale.status, sale.locked_atoms)

        resting = [u for u in found.get(spk, [])
                   if u["asset"] == sale.terms.token_asset]

        if resting:
            # Prefer the largest: a sale should only ever have one output
            # resting, but a stray payment to the address must not shrink it.
            u = max(resting, key=lambda x: x["atoms"])
            before = (sale.status, sale.locked_atoms,
                      sale.funding and sale.funding.get("txid"))
            sale.locked_atoms = u["atoms"]
            sale.funding = {"txid": u["txid"], "vout": u["vout"], "atoms": u["atoms"]}
            total = sale.terms.total_atoms or u["atoms"]
            sale.sold_atoms = max(0, total - u["atoms"])
            sale.status = S.LIVE if u["atoms"] >= total else S.PARTIAL
            self._misses.pop(project.slug, None)
            return before != (sale.status, sale.locked_atoms, u["txid"])

        # Nothing resting anywhere we can see. Before concluding that, wait for
        # a second look: a buy's remainder sits in the mempool for a block or
        # two, and the confirmed-set scan cannot see it either. Declaring a sale
        # finished on one blind reading would flap it in and out of existence.
        if sale.status == S.DRAFT:
            return False
        misses = self._misses.get(project.slug, 0) + 1
        self._misses[project.slug] = misses
        if misses < self.confirm_misses:
            return False
        was = sale.status
        if sale.funding and not self._funding_exists(sale.funding["txid"]):
            # The funding transaction itself is gone from the chain: this is a
            # reorg, not a completed sale.
            sale.mark_ghost()
        else:
            sale.locked_atoms = 0
            if sale.terms.total_atoms:
                sale.sold_atoms = sale.terms.total_atoms
            sale.status = S.SOLD_OUT
        return was != sale.status

    def _funding_exists(self, txid):
        """Is the funding transaction still part of the chain?

        Asked without -txindex, so this checks whether ANY of its outputs is
        still known. A transaction that has been fully spent looks the same as
        one that never existed, which is why a sale is only called a ghost when
        its own covenant output is gone AND nothing else of the funding
        transaction survives -- and why `mark_ghost` is reversible by simply
        locking again.
        """
        for vout in range(0, 8):
            try:
                if self.rpc.txout(txid, vout) is not None:
                    return True
            except Exception:
                return True          # cannot tell: assume it is there
        try:
            tx = self.rpc.call("getrawtransaction", txid, True)
            return bool(tx)
        except Exception:
            return False


def _out_atoms(out):
    """Atoms held by a `gettxout` result."""
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
