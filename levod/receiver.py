#!/usr/bin/env python3
"""The project's Lightning receiver: the half of a Lightning sale a project runs.

levod holds no keys. A Lightning sale needs what only the project can give: an
invoice from its Lightning node, and a signature under the sale's receiver key
that moves a lot into a lock under that invoice's payment hash (lnsale.py).
This process gives both, on the project's own machine, and does the rest of
the purchase itself, so a buyer needs nothing but BTC:

  1. An order gets an invoice whose secret only this process knows, and the
     project's Lightning node HOLDS any payment to it: the buyer's BTC is
     committed but nobody has it yet.
  2. Once a payment is held, the receiver locks the lot for the buyer under the
     invoice's hash. It pays the transaction's fee from its own coins, and
     funds the lock's fee pot (the bond lock), which pays the delivery's fee.
  3. Once the lock is in a block, it broadcasts the delivery, which needs no
     signature: the secret sends the lot to the buyer's address and the pot's
     remainder after its fee to the buyer too. Then it settles the payment.

A sale that vests is delivered another way, so that what the buyer receives
cannot move before the vesting date: the receiver pays the lot from the
project's Arca wallet as a vesting claim (Arca's `vest-1` leaf) to the buyer's
key, and settles only once the operator has released that claim, which from
then on is the buyer's whatever the receiver does: nobody can move it before
the vesting date, and only the buyer's key can after it (what nobody takes up
by the deadline goes back to the project). The tokens reach that wallet from
the sale itself: when it holds too few for a lot, the receiver moves the lot
and a little more (the operator's fee and its smallest leaf) out of the sale
to the wallet's own address and boards them, and pays the lot once they are
in. Each payment spends every coin of the token the wallet holds, the rest as
a vesting claim to the project's own key, since the operator takes no change
below its smallest leaf; the receiver takes those up once they vest. A
payment that cannot be backed so is failed back, and whatever was moved
stays in the wallet for the next lot.

The receiver is never paid without revealing the secret, and the secret alone
delivers the lot: if it settled without delivering, the buyer, now holding the
secret, can broadcast the same delivery (levod builds it) before the lock's
timeout. A payment it cannot back with a lock (the sale emptied, no coins for
the fee) is failed back to the buyer. A lock whose payment is gone before
delivery goes back to the sale after the timeout, and the pot to the receiver.

    python3 levod/receiver.py

Settings, from the environment:

  LEVO_RECEIVER_KEY_FILE     a file holding the receiver key's 32-byte secret, hex
  LEVO_RECEIVER_LIGHTNING    the Lightning node's command line, e.g.
                             "lightning-cli --network=testnet4". The node runs the
                             holdinvoice plugin (seqln's contrib/holdinvoice-seq).
  LEVO_RECEIVER_LEVO         the levod that lists the sales, e.g. http://127.0.0.1:8099
  LEVO_RECEIVER_SLUGS        the sales (slugs) this receiver serves, comma separated
                             (empty: any sale naming its key)
  LEVO_RECEIVER_RPC_URL      the Sequentia node (and _USER/_PASSWORD or _COOKIE)
  LEVO_RECEIVER_HRP          the chain's address prefix (default tb)
  LEVO_RECEIVER_STATE        where it keeps its orders and their secrets (mode 600)
  LEVO_RECEIVER_PORT         default 8098, on 127.0.0.1 unless LEVO_RECEIVER_BIND says otherwise
  LEVO_RECEIVER_INVOICE_EXPIRY   seconds an invoice stays payable (default 600)
  LEVO_RECEIVER_FEE_ATOMS    the fee of a lock and of a delivery, in the bond asset
                             (default: the sale's fee allowance)
  LEVO_RECEIVER_ARCA         the project's Arca wallet, the `arca` command line with its
                             directory, e.g. "arca --datadir /var/lib/levo-receiver/arca";
                             needed only for a sale that vests
  LEVO_RECEIVER_VEST_DAYS    days after the vesting date a buyer's wallet has to take a
                             vesting lot up before it goes back to the project (default 30;
                             the operator takes 7 to 400)

The receiver's own coins, which pay those fees and fund the pots, rest at the
taproot output of its key; `GET /health` names that address.
"""

import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import address as ADDR  # noqa: E402
import covenant as C  # noqa: E402
import lnsale as L  # noqa: E402
import rpc as RPCMOD  # noqa: E402
import secp256k1 as EC  # noqa: E402
import tx as TX  # noqa: E402

# A lot is never locked with less than this left between the invoice's expiry
# and the lock's timeout.
MIN_MARGIN_SECONDS = 1800

# A vesting lot is paid as a vesting claim only while the vesting date is at
# least this far ahead (median time): the claim's round must be final before
# it, and the operator refuses a date that has passed. Past it, a lot is
# delivered as any lot is, to the buyer's address.
VEST_MARGIN_SECONDS = 3 * 3600

# How long a paid vesting lot may wait for its tokens to come into the Arca
# wallet before the payment goes back.
VEST_PATIENCE_SECONDS = 3 * 3600

# What a top-up moves out of the sale beyond the lot: a fiftieth of it (the
# operator's fee for a distribution is at most a hundredth of what it pays, the
# wallet's own bound) and the operator's smallest leaf of the token. A
# distribution spends every coin of the token the wallet holds and leaves no
# change, because the operator takes none below its smallest leaf: the buyer's
# lot, and the rest as a claim to the project's own key, which that margin keeps
# at least a smallest leaf. The project takes its claims up once they vest.
STOCK_MARGIN_DIV = 50

# How often, at most, the receiver takes up its own claims once they vest.
OWN_CLAIMS_EVERY = 3600

# The receiver's states of a vesting lot after its payment is held.
VEST_STATES = ("stocking", "boarding", "distributing")


class ReceiverError(ValueError):
    pass


class Cln:
    """A Core Lightning node, through its command line, with the holdinvoice plugin."""

    def __init__(self, command):
        self.cmd = shlex.split(command)

    def call(self, method, **kw):
        args = self.cmd + ["-k", method] + ["%s=%s" % (k, v) for k, v in kw.items()]
        r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ReceiverError("the Lightning node refused %s: %s"
                                % (method, (r.stdout or r.stderr).strip()[:300]))
        return json.loads(r.stdout)

    def invoice(self, amount_msat, label, description, expiry, preimage):
        r = self.call("invoice", amount_msat=amount_msat, label=label, description=description,
                      expiry=expiry, preimage=preimage)
        return {"payment_hash": r["payment_hash"], "bolt11": r["bolt11"],
                "expires_at": int(r["expires_at"])}

    def hold(self, payment_hash, amount_msat):
        return self.call("holdinvoice", payment_hash=payment_hash, amount_msat=amount_msat)

    def held(self, payment_hash):
        return self.call("holdinvoicelookup", payment_hash=payment_hash)

    def settle(self, payment_hash, preimage):
        return self.call("holdinvoicesettle", payment_hash=payment_hash, preimage=preimage)

    def cancel(self, payment_hash):
        return self.call("holdinvoicecancel", payment_hash=payment_hash)


class Arca:
    """The project's Arca wallet, through the `arca` command line."""

    def __init__(self, command):
        self.cmd = shlex.split(command)

    def call(self, *args, stdin=None, timeout=900):
        r = subprocess.run(self.cmd + [str(a) for a in args], input=stdin, capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            raise ReceiverError("the Arca wallet refused %s: %s"
                                % (args[0], (r.stderr or r.stdout).strip()[-400:]))
        return json.loads(r.stdout)

    def address(self):
        return self.call("address")

    def sync(self):
        return self.call("sync")

    def board(self, asset, atoms, fee_asset):
        return self.call("board", asset, int(atoms), "--fee-asset", fee_asset)

    def quote(self, asset, claims):
        return self.call("distribute", "-", "--asset", asset, "--quote",
                         stdin="".join("%s %d\n" % (k, int(a)) for k, a in claims))

    def distribute(self, asset, claims, vest, days, fee):
        return self.call("distribute", "-", "--asset", asset, "--vest", int(vest), "--days",
                         int(days), "--shown", str(fee),
                         stdin="".join("%s %d\n" % (k, int(a)) for k, a in claims))

    def coins(self):
        return self.call("coins")

    def info(self):
        return self.call("info")

    def claim(self):
        return self.call("claim")

    def distributions(self):
        return self.call("distributions")


class Receiver:
    def __init__(self, secret, ln, node, sale_of, state_path, slugs=None, invoice_expiry=600,
                 fee_atoms=None, now=time.time, arca=None, vest_days=30):
        self.secret = int(secret, 16) if isinstance(secret, str) else int(secret)
        self.xonly = EC.xonly_pubkey(self.secret).hex()
        # The receiver's own coins rest at its key's taproot output, spent by key.
        self.own_spk = bytes.fromhex("5120" + self.xonly)
        self.ln = ln
        self.node = node
        self.sale_of = sale_of               # slug -> (covenant.SaleTerms, {txid, vout, atoms})
        self.state_path = Path(state_path)
        self.slugs = set(slugs) if slugs else None
        self.invoice_expiry = int(invoice_expiry)
        self.fee_atoms = fee_atoms
        self.now = now
        self.arca = arca
        self.vest_days = int(vest_days)
        if not 7 <= self.vest_days <= 400:
            raise ReceiverError("a vesting lot's take-up window is 7 to 400 days")
        self.lock = threading.RLock()
        self.lots = {}
        self.sale_coins = {}                 # slug -> the sale coin after this receiver's last lock
        self.spent_own = set()               # own coins spent by locks not yet in a block
        if self.state_path.is_file():
            self.lots = json.loads(self.state_path.read_text()).get("lots", {})

    def _save(self):
        tmp = self.state_path.with_suffix(".tmp")
        old = os.umask(0o077)
        try:
            tmp.write_text(json.dumps({"lots": self.lots}, indent=1, sort_keys=True))
        finally:
            os.umask(old)
        os.replace(tmp, self.state_path)

    def _sale(self, slug):
        if self.slugs is not None and slug not in self.slugs:
            raise ReceiverError("this receiver does not serve the sale %r" % slug)
        terms, funding = self.sale_of(slug)
        if terms.lightning is None:
            raise ReceiverError("that sale is not paid over Lightning")
        if terms.lightning.receiver_xonly != self.xonly:
            raise ReceiverError("that sale names another receiver key")
        if terms.lightning.timeout_seconds < self.invoice_expiry + MIN_MARGIN_SECONDS:
            raise ReceiverError("the sale's lock timeout is too short for this receiver's invoices")
        return terms, C.derive(terms), funding

    def _fee(self, terms):
        return int(self.fee_atoms if self.fee_atoms is not None else terms.lightning.fee_allowance)

    # --- an order ---------------------------------------------------------

    def _vests(self, terms):
        """Whether a lot of this sale is paid as a vesting claim now: the sale
        vests and its date is still far enough ahead."""
        v = terms.lightning.vest_locktime
        if not v:
            return False
        tip = self.node.call("getblockchaininfo")
        return int(tip.get("mediantime", 0)) + VEST_MARGIN_SECONDS < v

    def invoice(self, slug, token_atoms, dest_spk, refund_spk, buyer_key=None):
        terms, cov, _ = self._sale(slug)
        token_atoms = int(token_atoms)
        if token_atoms < terms.min_lot:
            raise ReceiverError("below the sale's minimum")
        L._witness(bytes.fromhex(dest_spk))
        L._witness(bytes.fromhex(refund_spk))
        vest = None
        if self._vests(terms):
            if self.arca is None:
                raise ReceiverError("this sale vests and this receiver has no Arca wallet to pay "
                                    "vesting lots from")
            k = bytes.fromhex(str(buyer_key or ""))
            if len(k) != 33 or k[0] not in (2, 3):
                raise ReceiverError("this sale vests: name the buyer's key (a compressed public "
                                    "key) the lot is paid to")
            if EC.lift_x(int.from_bytes(k[1:], "big")) is None:
                raise ReceiverError("the buyer's key is not a public key")
            vest = terms.lightning.vest_locktime
        sats = terms.lightning.sats_for(token_atoms)
        preimage = secrets.token_bytes(32)
        h = hashlib.sha256(preimage).hexdigest()
        # Held before the invoice exists, so no payment can arrive unheld.
        self.ln.hold(h, sats * 1000)
        inv = self.ln.invoice(sats * 1000, "levo-%s-%s" % (slug, h[:16]),
                              "%s: %d token atoms" % (slug, token_atoms), self.invoice_expiry,
                              preimage.hex())
        if inv["payment_hash"] != h:
            raise ReceiverError("the Lightning node made an invoice under another hash")
        rec = {"slug": slug, "token_atoms": token_atoms, "dest_spk": dest_spk,
               "refund_spk": refund_spk, "sats": sats, "bolt11": inv["bolt11"],
               "expires_at": inv["expires_at"], "state": "issued", "preimage": preimage.hex(),
               "created_at": int(self.now())}
        if vest:
            rec.update(vest=vest, buyer_key=str(buyer_key).lower())
        with self.lock:
            self.lots[h] = rec
            self._save()
        out = {k: v for k, v in rec.items() if k != "preimage"}
        return dict(out, payment_hash=h, amount_msat=sats * 1000)

    def status(self, payment_hash):
        rec = self.lots.get(payment_hash)
        if not rec:
            raise ReceiverError("this receiver issued no invoice with that hash")
        out = {"state": rec["state"], "held": False}
        try:
            out["held"] = self.ln.held(payment_hash).get("state") == "accepted"
        except Exception:
            pass
        for k in ("reserve_txid", "bond_vout", "claim_txid", "rerest_txid", "stock_txid",
                  "distribution_id", "vest", "buyer_key", "error"):
            if rec.get(k) is not None:
                out[k] = rec[k]
        if rec["state"] == "delivered":
            # Paid: the buyer holds the secret already, and with it anyone can
            # deliver the lot, which can only go to the buyer.
            out["preimage"] = rec["preimage"]
        return out

    # --- following every order ---------------------------------------------

    def poll(self):
        done = []
        if self.arca is not None and any(r["state"] in VEST_STATES[1:] for r in self.lots.values()):
            try:
                self.arca.sync()        # hands over forfeits, brings boards in
            except Exception as e:
                sys.stderr.write("receiver: the Arca wallet's sync failed: %s\n" % e)
        self._take_up_own_claims()
        for h, rec in list(self.lots.items()):
            if rec["state"] not in ("issued", "reserved") + VEST_STATES:
                continue
            try:
                r = self._follow(h, rec)
                if r:
                    done.append(r)
            except Exception as e:      # one order must not stop the others
                rec["error"] = str(e)[:300]
        with self.lock:
            self._save()
        return done

    def _follow(self, h, rec):
        held = self.ln.held(h)
        hstate = held.get("state")
        if rec["state"] in VEST_STATES:
            return self._follow_vest(h, rec, hstate)
        if rec["state"] == "issued":
            if hstate == "accepted" and int(held.get("received_msat") or 0) >= rec["sats"] * 1000:
                try:
                    if rec.get("vest"):
                        return self._vest_begin(h, rec)
                    self._reserve(h, rec)
                except Exception as e:
                    # A payment the receiver cannot back with a lock goes back.
                    self.ln.cancel(h)
                    rec.update(state="cancelled", error=str(e)[:300])
                    return ("cancelled", h, str(e)[:120])
                return ("reserved", h, rec["reserve_txid"])
            if self.now() > rec["expires_at"] + 60 and hstate != "accepted":
                try:
                    self.ln.cancel(h)
                except Exception:
                    pass
                rec["state"] = "expired"
                return ("expired", h, None)
            return None
        # reserved: deliver once the lock is in a block and the payment is still held.
        lot = self.node.txout(rec["reserve_txid"], 0)
        if lot is None:
            return None                  # not seen yet, or spent already
        terms, cov, _ = self._sale(rec["slug"])
        lock = cov.lock(bytes.fromhex(h), rec["dest_spk"], rec["refund_spk"])
        if int(lot.get("confirmations") or 0) < 1:
            return None
        if hstate == "accepted":
            t = L.build_claim(lock, rec["reserve_txid"], 0, rec["token_atoms"], rec["bond_vout"],
                              terms.lightning.bond_atoms, bytes.fromhex(rec["preimage"]),
                              self._fee(terms))
            txid = self.node.call("sendrawtransaction", t.hex())
            rec.update(claim_txid=txid)
            self.ln.settle(h, rec["preimage"])
            rec["state"] = "delivered"
            return ("delivered", h, txid)
        # The payment is gone (failed back or expired): the lot waits out its
        # timeout and goes back to the sale.
        tip = self.node.call("getblockchaininfo")
        if int(tip.get("mediantime", 0)) < self._lock_started(lot) + lock.timeout_seconds:
            return None
        t, spent = L.build_rerest(lock, rec["reserve_txid"], 0, rec["token_atoms"],
                                  rec["bond_vout"], terms.lightning.bond_atoms, self.own_spk,
                                  self._fee(terms))
        sighash = TX.taproot_sighash(t, spent, 1, self.node.call("getblockhash", 0),
                                     leaf_script=lock.forfeit_leaf)
        t.vin[1].witness = lock.bond.witness("forfeit", EC.schnorr_sign(sighash, self.secret))
        try:
            txid = self.node.call("sendrawtransaction", t.hex())
        except Exception as e:
            if "non-BIP68-final" in str(e):
                return None
            raise
        rec.update(state="returned", rerest_txid=txid)
        return ("returned", h, txid)

    def _reserve(self, h, rec):
        """Lock the lot under `h`, paying the fee and the pot from own coins."""
        terms, cov, funding = self._sale(rec["slug"])
        ln = terms.lightning
        fee = self._fee(terms)
        coin = self._sale_coin(rec["slug"], cov, funding)
        own = self._own_coins(ln.bond_asset, ln.bond_atoms + fee)
        lock = cov.lock(bytes.fromhex(h), rec["dest_spk"], rec["refund_spk"])
        t, spent, bond_vout = L.build_reserve(cov, coin, rec["token_atoms"], lock, own,
                                              self.own_spk, fee)
        genesis = self.node.call("getblockhash", 0)
        sig = EC.schnorr_sign(TX.taproot_sighash(t, spent, 0, genesis, leaf_script=cov.reserve_leaf),
                              self.secret)
        t.vin[0].witness = cov.reserve_witness(sig)
        for i in range(1, len(t.vin)):
            t.vin[i].witness = [EC.schnorr_sign(TX.taproot_sighash(t, spent, i, genesis), self.secret)]
        txid = self.node.call("sendrawtransaction", t.hex())
        left = int(coin["atoms"]) - rec["token_atoms"]
        self.sale_coins[rec["slug"]] = {"txid": txid, "vout": 1, "atoms": left} if left else None
        self.spent_own.update((c["txid"], c["vout"]) for c in own)
        rec.update(state="reserved", reserve_txid=txid, bond_vout=bond_vout)

    # --- a vesting lot ------------------------------------------------------

    def _fail_back(self, h, rec, why):
        try:
            self.ln.cancel(h)
        except Exception:
            pass
        rec.update(state="cancelled", error=str(why)[:300])
        return ("cancelled", h, str(why)[:120])

    def _vest_begin(self, h, rec):
        """A vesting lot's payment is held: pay the lot from the Arca wallet
        when it holds enough, else move it there from the sale first."""
        terms, cov, funding = self._sale(rec["slug"])
        rec["paid_at"] = int(self.now())
        try:
            plan = self._plan(terms, rec)
        except ReceiverError:
            plan = None
        try:
            if plan is not None:
                return self._distribute(h, rec, terms, plan)
            self._stock(h, rec, terms, cov, funding)
        except Exception as e:
            return self._fail_back(h, rec, e)
        return ("stocking", h, rec["stock_txid"])

    def _stock(self, h, rec, terms, cov, funding):
        """Move the lot and its fee's margin out of the sale to the Arca
        wallet's own address, with a coin for the board's fee."""
        ln = terms.lightning
        fee = self._fee(terms)
        coin = self._sale_coin(rec["slug"], cov, funding)
        lot = rec["token_atoms"]
        need = lot + -(-lot // STOCK_MARGIN_DIV) + self._min_leaf(terms.token_asset)
        have = int(coin["atoms"])
        if have < lot:
            raise ReceiverError("the sale holds fewer tokens than this lot")
        if have - need < terms.min_lot:
            need = have                      # take it all rather than leave less than a lot
        to = self.arca.address()
        to_spk = bytes.fromhex(to["script_pubkey"])
        own = self._own_coins(ln.bond_asset, ln.bond_atoms + fee)
        t, spent = L.build_stock(cov, coin, need, to_spk, ln.bond_asset, ln.bond_atoms, own,
                                 self.own_spk, fee)
        genesis = self.node.call("getblockhash", 0)
        sig = EC.schnorr_sign(TX.taproot_sighash(t, spent, 0, genesis, leaf_script=cov.reserve_leaf),
                              self.secret)
        t.vin[0].witness = cov.reserve_witness(sig)
        for i in range(1, len(t.vin)):
            t.vin[i].witness = [EC.schnorr_sign(TX.taproot_sighash(t, spent, i, genesis), self.secret)]
        txid = self.node.call("sendrawtransaction", t.hex())
        left = have - need
        self.sale_coins[rec["slug"]] = {"txid": txid, "vout": 1, "atoms": left} if left else None
        self.spent_own.update((c["txid"], c["vout"]) for c in own)
        rec.update(state="stocking", stock_txid=txid, stock_atoms=need)

    def _min_leaf(self, asset):
        """The operator's smallest leaf of `asset`, as it publishes it."""
        for a in (self.arca.info().get("server_info") or {}).get("assets") or []:
            if (a.get("asset") or "").lower() == asset:
                return int(a.get("min_leaf") or 0)
        raise ReceiverError("the operator does not serve this sale's token")

    def _own_key(self):
        """A key of the project's Arca wallet, for its own claims: the key of
        an address it handed out, which its claim lookup names."""
        if not getattr(self, "own_key", None):
            self.own_key = self.arca.address()["key"]
        return self.own_key

    def _plan(self, terms, rec):
        """The claims that pay this lot and spend every live coin of the token
        the wallet holds, with the operator's fee for them: the buyer's lot,
        and the rest to the project's own key. Raises ReceiverError when the
        wallet holds too little for that (the rest would be below the
        operator's smallest leaf, or nothing pays the lot)."""
        token = terms.token_asset
        total = sum(int(c.get("value") or 0) for c in self.arca.coins()
                    if (c.get("asset") or "").lower() == token and c.get("state") == "live")
        lot = int(rec["token_atoms"])
        if total < lot:
            raise ReceiverError("the project's Arca wallet holds too few tokens for this lot")
        # The lot alone's fee starts the estimate (the fee follows the coins
        # given up); the operator may refuse that quote for its change.
        try:
            fee = int(self.arca.quote(token, [(rec["buyer_key"], lot)])["fee"])
        except ReceiverError:
            fee = 0
        for _ in range(8):
            rest = total - lot - fee
            if rest < 0:
                raise ReceiverError("the project's Arca wallet holds too few tokens for this lot")
            claims = [(rec["buyer_key"], lot)] + ([(self._own_key(), rest)] if rest else [])
            try:
                quoted = int(self.arca.quote(token, claims)["fee"])
            except ReceiverError:
                if rest and rest < self._min_leaf(token):
                    raise                              # a rest below a leaf: move more in first
                fee = fee * 2 + 1                      # the fee is more than estimated
                continue
            if quoted == fee:
                return {"claims": claims, "fee": fee}
            fee = quoted
        raise ReceiverError("the operator's fee for this lot did not settle")

    def _distribute(self, h, rec, terms, plan):
        """Submit the distribution. When the wallet must first pay itself one
        coin of the exact amount (it holds several), that payment leaves a
        margin for its own fees out of the asset, which the wallet names
        ("paying P takes Q"): the project's own claim gives it up, and the
        distribution is quoted and submitted again."""
        token = terms.token_asset
        claims, fee = list(plan["claims"]), plan["fee"]
        for _ in range(3):
            try:
                d = self.arca.distribute(token, claims, rec["vest"], self.vest_days, fee)
                break
            except ReceiverError as e:
                m = re.search(r"paying (\d+) takes (\d+)", str(e))
                if not m or len(claims) < 2:
                    raise
                short = int(m.group(2)) - int(m.group(1))
                own = claims[1][1] - short
                if short <= 0 or own < self._min_leaf(token):
                    raise
                claims[1] = (claims[1][0], own)
                fee = int(self.arca.quote(token, claims)["fee"])
        else:
            raise ReceiverError("the wallet's margins for this lot did not settle")
        rec.update(state="distributing", distribution_id=d["distribution"])
        return ("distributing", h, d["distribution"])

    def _follow_vest(self, h, rec, hstate):
        terms, cov, _ = self._sale(rec["slug"])
        if rec["state"] != "distributing" and hstate != "accepted":
            # The payment went away before the lot was paid: nothing is owed.
            # What came into the Arca wallet stays there for the next lot.
            rec.update(state="cancelled", error="the payment went away before the lot was paid")
            return ("cancelled", h, None)
        if rec["state"] == "stocking":
            out = self.node.txout(rec["stock_txid"], 0)
            if out is None or int(out.get("confirmations") or 0) < 1:
                return self._patience(h, rec)
            try:
                self.arca.board(terms.token_asset, rec["stock_atoms"], terms.lightning.bond_asset)
            except Exception as e:
                return self._fail_back(h, rec, e)
            rec["state"] = "boarding"
            return ("boarding", h, None)
        if rec["state"] == "boarding":
            try:
                plan = self._plan(terms, rec)
            except ReceiverError:
                return self._patience(h, rec)     # not in yet
            try:
                return self._distribute(h, rec, terms, plan)
            except Exception as e:
                return self._fail_back(h, rec, e)
        # distributing: settle once the operator has released the buyer's claim.
        mine = None
        for d in self.arca.distributions():
            if d.get("distribution_id") == rec["distribution_id"]:
                mine = d
        if mine is None or mine.get("error"):
            return None
        states = [c.get("state") for c in mine.get("claims") or []]
        if not states or any(s not in ("vesting", "open", "claimed") for s in states):
            return None
        if hstate == "accepted":
            self.ln.settle(h, rec["preimage"])
            rec["state"] = "delivered"
            return ("delivered", h, rec["distribution_id"])
        rec.update(state="delivered", error="the payment went away after the lot was paid")
        return ("delivered", h, rec["distribution_id"])

    def _take_up_own_claims(self):
        """Once a lot it paid has vested, the project's own claims (the rest of
        each distribution) are taken up, at most once an hour."""
        if self.arca is None:
            return
        paid = [r for r in self.lots.values() if r.get("distribution_id")]
        if not paid or self.now() - getattr(self, "own_claims_at", 0) < OWN_CLAIMS_EVERY:
            return
        try:
            mtp = int(self.node.call("getblockchaininfo").get("mediantime", 0))
            if not any(int(r.get("vest") or 0) <= mtp for r in paid):
                return
            self.own_claims_at = self.now()
            self.arca.claim()
        except Exception as e:
            sys.stderr.write("receiver: taking up the project's own claims failed: %s\n" % e)

    def _patience(self, h, rec):
        if self.now() - int(rec.get("paid_at") or self.now()) > VEST_PATIENCE_SECONDS:
            return self._fail_back(h, rec, "the tokens did not come into the project's Arca wallet "
                                           "in time")
        return None

    def _sale_coin(self, slug, cov, funding):
        """The coin the sale's tokens rest at now: the one this receiver's last
        lock left, or levod's, or the largest the chain holds at the address."""
        for c in (self.sale_coins.get(slug), funding):
            if c and c.get("txid") and self.node.txout(c["txid"], int(c["vout"])) is not None:
                return c
        found = self.node.call("scantxoutset", "start", ["raw(%s)" % cov.spk_hex])
        best = None
        for u in found.get("unspents") or []:
            if (u.get("asset") or "").lower() == cov.terms.token_asset \
                    and self.node.txout(u["txid"], u["vout"]) is not None:
                atoms = RPCMOD.to_atoms(u["amount"])
                if best is None or atoms > best["atoms"]:
                    best = {"txid": u["txid"], "vout": u["vout"], "atoms": atoms}
        if not best:
            raise ReceiverError("the sale holds no tokens")
        return best

    def _own_coins(self, asset, need):
        found = self.node.call("scantxoutset", "start", ["raw(%s)" % self.own_spk.hex()])
        picked, total = [], 0
        for u in sorted(found.get("unspents") or [], key=lambda u: -float(u["amount"])):
            if (u.get("asset") or "").lower() != asset or (u["txid"], u["vout"]) in self.spent_own:
                continue
            if self.node.txout(u["txid"], u["vout"]) is None:
                continue
            picked.append({"txid": u["txid"], "vout": u["vout"], "asset": asset,
                           "value_atoms": RPCMOD.to_atoms(u["amount"]),
                           "script_pubkey": self.own_spk.hex()})
            total += picked[-1]["value_atoms"]
            if total >= need:
                return picked
        raise ReceiverError("the receiver holds %d atoms of the bond asset at its own address "
                            "and a lock needs %d" % (total, need))

    def _lock_started(self, lot):
        height = int(self.node.call("getblockcount")) - int(lot["confirmations"]) + 1
        prev = self.node.call("getblockheader", self.node.call("getblockhash", height - 1))
        return int(prev["mediantime"])


# --- the service -----------------------------------------------------------

def _levod_sales(base):
    cache = {}

    def sale_of(slug):
        hit = cache.get(slug)
        if hit and hit[0] > time.time():
            return hit[1]
        with urllib.request.urlopen("%s/api/projects/%s" % (base, slug), timeout=20) as r:
            d = json.loads(r.read())
        sale = (d.get("project") or d).get("sale") or {}
        terms = C.SaleTerms.from_json(sale.get("terms"))
        f = sale.get("funding") or {}
        funding = {"txid": f.get("txid"), "vout": f.get("vout"),
                   "atoms": int(sale.get("locked_atoms") or f.get("atoms") or 0)} if f else None
        cache[slug] = (time.time() + 20, (terms, funding))
        return terms, funding
    return sale_of


def make_handler(rcv, hrp="tb"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            sys.stderr.write("receiver %s\n" % (fmt % a))

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ReceiverError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            try:
                if self.path == "/health":
                    return self._send(200, {"ok": True, "receiver_xonly": rcv.xonly,
                                            "own_address": ADDR.from_script_pubkey(rcv.own_spk.hex(), hrp)})
                if self.path.startswith("/invoice/"):
                    return self._send(200, rcv.status(self.path.split("/")[2]))
                return self._send(404, {"error": "no such path"})
            except ReceiverError as e:
                return self._send(400, {"error": str(e)})

        def do_POST(self):
            try:
                b = self._body()
                if self.path == "/invoice":
                    return self._send(200, rcv.invoice(b["slug"], b["token_atoms"], b["dest_spk"],
                                                       b.get("refund_spk") or b["dest_spk"],
                                                       b.get("buyer_key")))
                return self._send(404, {"error": "no such path"})
            except (ReceiverError, ValueError, KeyError) as e:
                return self._send(400, {"error": str(e)})
    return H


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__.strip())
        return 0
    env = os.environ
    secret = Path(env["LEVO_RECEIVER_KEY_FILE"]).read_text().strip()
    node = RPCMOD.NodeRPC(url=env.get("LEVO_RECEIVER_RPC_URL"), user=env.get("LEVO_RECEIVER_RPC_USER"),
                          password=env.get("LEVO_RECEIVER_RPC_PASSWORD"),
                          cookie=env.get("LEVO_RECEIVER_RPC_COOKIE"))
    hrp = env.get("LEVO_RECEIVER_HRP", "tb")
    slugs = [s.strip() for s in env.get("LEVO_RECEIVER_SLUGS", "").split(",") if s.strip()] or None
    fee = env.get("LEVO_RECEIVER_FEE_ATOMS")
    arca = Arca(env["LEVO_RECEIVER_ARCA"]) if env.get("LEVO_RECEIVER_ARCA") else None
    rcv = Receiver(secret, Cln(env["LEVO_RECEIVER_LIGHTNING"]), node,
                   _levod_sales(env["LEVO_RECEIVER_LEVO"].rstrip("/")),
                   env.get("LEVO_RECEIVER_STATE", "receiver-state.json"), slugs,
                   int(env.get("LEVO_RECEIVER_INVOICE_EXPIRY", "600")),
                   int(fee) if fee else None, arca=arca,
                   vest_days=int(env.get("LEVO_RECEIVER_VEST_DAYS", "30")))

    def loop():
        while True:
            try:
                for what in rcv.poll():
                    sys.stderr.write("receiver: %s %s %s\n" % what)
            except Exception as e:
                sys.stderr.write("receiver: poll failed: %s\n" % e)
            time.sleep(int(env.get("LEVO_RECEIVER_POLL", "5")))
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((env.get("LEVO_RECEIVER_BIND", "127.0.0.1"),
                               int(env.get("LEVO_RECEIVER_PORT", "8098"))), make_handler(rcv, hrp))
    sys.stderr.write("receiver: serving key %s\n" % rcv.xonly)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
