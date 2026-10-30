#!/usr/bin/env python3
"""The project's Lightning receiver: the half of a Lightning sale a project runs.

levod holds no keys. A Lightning sale needs two things only the project can
give: an invoice from its Lightning node, whose secret the project's node
keeps until the buyer pays, and a signature under the sale's receiver key that
moves a lot into a lock under that invoice's payment hash (lnsale.py). This
process gives both, on the project's own machine, and then follows every lock
it signed:

  * paid  -> it broadcasts the claim, which needs no signature: the secret
             delivers the lot to the buyer and returns the bond, so the buyer
             receives the tokens without doing anything after paying;
  * never paid, past the timeout -> it puts the lot back in the sale and keeps
             the bond, under its own signature.

It signs a reservation only for an invoice it issued, only for the lot that
invoice prices, and only for a transaction it rebuilt itself from the coins
named, so a levod that lies cannot have it sign anything else.

    python3 levod/receiver.py

Settings, from the environment:

  LEVO_RECEIVER_KEY_FILE     a file holding the receiver key's 32-byte secret, hex
  LEVO_RECEIVER_LIGHTNING    the Lightning node's command line, e.g.
                             "lightning-cli --network=testnet4"
  LEVO_RECEIVER_LEVO         the levod that lists the sale, e.g. http://127.0.0.1:8099
  LEVO_RECEIVER_SLUGS        the sales (slugs) this receiver serves, comma separated
  LEVO_RECEIVER_BOND_TO      the address forfeited bonds are paid to
  LEVO_RECEIVER_RPC_URL      the Sequentia node (and _USER/_PASSWORD or _COOKIE)
  LEVO_RECEIVER_STATE        where it keeps what it issued (default ./receiver-state.json)
  LEVO_RECEIVER_PORT         default 8098, on 127.0.0.1 unless LEVO_RECEIVER_BIND says otherwise
  LEVO_RECEIVER_INVOICE_EXPIRY   seconds an invoice stays payable (default 600)
  LEVO_RECEIVER_FEE_ATOMS    the fee a claim or re-rest pays from the bond
                             (default: the sale's fee allowance)
"""

import hashlib
import json
import os
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

# A lock is never signed with less than this left between the invoice's expiry
# and the lock's timeout: the buyer's payment can be held until it expires,
# and the claim needs time to confirm after it.
MIN_MARGIN_SECONDS = 1800


class ReceiverError(ValueError):
    pass


class Cln:
    """A Core Lightning node, through its command line."""

    def __init__(self, command):
        self.cmd = shlex.split(command)

    def call(self, method, **kw):
        args = self.cmd + ["-k", method] + ["%s=%s" % (k, v) for k, v in kw.items()]
        r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ReceiverError("the Lightning node refused %s: %s"
                                % (method, (r.stdout or r.stderr).strip()[:300]))
        return json.loads(r.stdout)

    def invoice(self, amount_msat, label, description, expiry):
        r = self.call("invoice", amount_msat=amount_msat, label=label,
                      description=description, expiry=expiry)
        return {"payment_hash": r["payment_hash"], "bolt11": r["bolt11"],
                "expires_at": int(r["expires_at"])}

    def lookup(self, payment_hash):
        r = self.call("listinvoices", payment_hash=payment_hash)
        inv = (r.get("invoices") or [None])[0]
        if not inv:
            return {"status": "unknown"}
        out = {"status": inv.get("status")}
        if inv.get("status") == "paid" and inv.get("payment_preimage"):
            out["preimage"] = inv["payment_preimage"]
        return out


class Receiver:
    def __init__(self, secret, ln, node, terms_of, state_path, bond_to_spk, slugs=None,
                 invoice_expiry=600, fee_atoms=None, now=time.time):
        self.secret = int(secret, 16) if isinstance(secret, str) else int(secret)
        self.xonly = EC.xonly_pubkey(self.secret).hex()
        self.ln = ln
        self.node = node
        self.terms_of = terms_of            # slug -> covenant.SaleTerms
        self.state_path = Path(state_path)
        self.bond_to_spk = bytes.fromhex(bond_to_spk) if isinstance(bond_to_spk, str) else bond_to_spk
        self.slugs = set(slugs) if slugs else None
        self.invoice_expiry = int(invoice_expiry)
        self.fee_atoms = fee_atoms
        self.now = now
        self.lock = threading.RLock()
        self.lots = {}
        if self.state_path.is_file():
            self.lots = json.loads(self.state_path.read_text()).get("lots", {})

    def _save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"lots": self.lots}, indent=1, sort_keys=True))
        os.replace(tmp, self.state_path)

    def _sale(self, slug):
        if self.slugs is not None and slug not in self.slugs:
            raise ReceiverError("this receiver does not serve the sale %r" % slug)
        terms = self.terms_of(slug)
        if terms.lightning is None:
            raise ReceiverError("that sale is not paid over Lightning")
        if terms.lightning.receiver_xonly != self.xonly:
            raise ReceiverError("that sale names another receiver key")
        if terms.lightning.timeout_seconds < self.invoice_expiry + MIN_MARGIN_SECONDS:
            raise ReceiverError("the sale's lock timeout is too short for this receiver's invoices")
        return terms, C.derive(terms)

    # --- issuing ----------------------------------------------------------

    def invoice(self, slug, token_atoms, dest_spk, refund_spk):
        terms, cov = self._sale(slug)
        token_atoms = int(token_atoms)
        if token_atoms < terms.min_lot:
            raise ReceiverError("below the sale's minimum")
        L._witness(bytes.fromhex(dest_spk))
        L._witness(bytes.fromhex(refund_spk))
        sats = terms.lightning.sats_for(token_atoms)
        label = "levo-%s-%s" % (slug, secrets.token_hex(8))
        inv = self.ln.invoice(sats * 1000, label, "%s: %d token atoms" % (slug, token_atoms),
                              self.invoice_expiry)
        rec = {"slug": slug, "token_atoms": token_atoms, "dest_spk": dest_spk,
               "refund_spk": refund_spk, "sats": sats, "bolt11": inv["bolt11"],
               "expires_at": inv["expires_at"], "state": "issued", "created_at": int(self.now())}
        with self.lock:
            self.lots[inv["payment_hash"]] = rec
            self._save()
        return dict(rec, payment_hash=inv["payment_hash"], amount_msat=sats * 1000)

    def sign_reserve(self, payment_hash, sale_coin, buyer_inputs, change_spk, fee_atoms):
        """Rebuild the reservation from the coins named, check each against
        the node, and sign input 0's RESERVE leaf."""
        with self.lock:
            rec = self.lots.get(payment_hash)
            if not rec:
                raise ReceiverError("this receiver issued no invoice with that hash")
            if rec["state"] not in ("issued", "signed"):
                raise ReceiverError("that reservation is %s" % rec["state"])
            if self.now() > rec["expires_at"]:
                raise ReceiverError("that invoice has expired; reserve again")
        terms, cov = self._sale(rec["slug"])
        coin = self.node.txout(sale_coin["txid"], int(sale_coin["vout"]))
        if not coin or (coin.get("scriptPubKey") or {}).get("hex") != cov.spk_hex:
            raise ReceiverError("the sale coin named is not unspent at the sale address")
        if (coin.get("asset") or "").lower() != terms.token_asset \
                or RPCMOD.to_atoms(coin["value"]) != int(sale_coin["atoms"]):
            raise ReceiverError("the sale coin named is not what the chain holds")
        checked = []
        for c in buyer_inputs:
            out = self.node.txout(c["txid"], int(c["vout"]))
            if not out or out.get("valuecommitment") or out.get("assetcommitment"):
                raise ReceiverError("a buyer coin is spent, missing or blinded")
            checked.append({"txid": c["txid"], "vout": int(c["vout"]),
                            "asset": out["asset"].lower(),
                            "value_atoms": RPCMOD.to_atoms(out["value"]),
                            "script_pubkey": out["scriptPubKey"]["hex"]})
        lock = cov.lock(bytes.fromhex(payment_hash), rec["dest_spk"], rec["refund_spk"])
        t, spent, bond_vout = L.build_reserve(cov, {"txid": sale_coin["txid"],
                                                    "vout": int(sale_coin["vout"]),
                                                    "atoms": int(sale_coin["atoms"])},
                                              rec["token_atoms"], lock, checked,
                                              bytes.fromhex(change_spk), int(fee_atoms))
        sighash = TX.taproot_sighash(t, spent, 0, self.node.call("getblockhash", 0),
                                     leaf_script=cov.reserve_leaf)
        sig = EC.schnorr_sign(sighash, self.secret)
        with self.lock:
            rec.update(state="signed", reserve_txid=t.txid(), bond_vout=bond_vout,
                       lot_vout=0, lot_atoms=rec["token_atoms"],
                       bond_atoms=terms.lightning.bond_atoms)
            self._save()
        return {"signature": sig.hex(), "txid": t.txid()}

    # --- following ----------------------------------------------------------

    def status(self, payment_hash):
        rec = self.lots.get(payment_hash)
        if not rec:
            raise ReceiverError("this receiver issued no invoice with that hash")
        paid = self.ln.lookup(payment_hash)
        out = {"state": rec["state"], "paid": paid.get("status") == "paid",
               "invoice": paid.get("status")}
        if out["paid"]:
            # Paid: the buyer holds the secret already, and publishing it lets
            # anyone deliver the lot, which can only go to the buyer.
            out["preimage"] = paid.get("preimage")
        for k in ("reserve_txid", "claim_txid", "rerest_txid"):
            if rec.get(k):
                out[k] = rec[k]
        return out

    def poll(self):
        """Claim every paid lock, re-rest every unpaid one past its timeout."""
        done = []
        for h, rec in list(self.lots.items()):
            if rec["state"] != "signed":
                continue
            try:
                done.append(self._follow(h, rec))
            except Exception as e:      # one bad lock must not stop the others
                rec["last_error"] = str(e)[:300]
        with self.lock:
            self._save()
        return [d for d in done if d]

    def _follow(self, h, rec):
        lot = self.node.txout(rec["reserve_txid"], rec["lot_vout"])
        if lot is None:
            return None          # not broadcast yet, or already spent
        terms, cov = self._sale(rec["slug"])
        lock = cov.lock(bytes.fromhex(h), rec["dest_spk"], rec["refund_spk"])
        fee = int(self.fee_atoms if self.fee_atoms is not None else terms.lightning.fee_allowance)
        paid = self.ln.lookup(h)
        if paid.get("status") == "paid" and paid.get("preimage"):
            t = L.build_claim(lock, rec["reserve_txid"], rec["lot_vout"], rec["lot_atoms"],
                              rec["bond_vout"], rec["bond_atoms"],
                              bytes.fromhex(paid["preimage"]), fee)
            txid = self.node.call("sendrawtransaction", t.hex())
            rec.update(state="claimed", claim_txid=txid)
            return ("claimed", h, txid)
        if paid.get("status") == "paid":
            return None
        if lot.get("confirmations", 0) < 1:
            return None
        tip = self.node.call("getblockchaininfo")
        if int(tip.get("mediantime", 0)) < self._lock_started(lot) + lock.timeout_seconds:
            return None
        t, spent = L.build_rerest(lock, rec["reserve_txid"], rec["lot_vout"], rec["lot_atoms"],
                                  rec["bond_vout"], rec["bond_atoms"], self.bond_to_spk, fee)
        sighash = TX.taproot_sighash(t, spent, 1, self.node.call("getblockhash", 0),
                                     leaf_script=lock.forfeit_leaf)
        t.vin[1].witness = lock.bond.witness("forfeit", EC.schnorr_sign(sighash, self.secret))
        try:
            txid = self.node.call("sendrawtransaction", t.hex())
        except Exception as e:
            if "non-BIP68-final" in str(e):
                return None      # the median time has not caught up yet
            raise
        rec.update(state="rerested", rerest_txid=txid)
        return ("rerested", h, txid)

    def _lock_started(self, lot):
        """The median time a relative lock counts from: that of the block
        before the one the lock confirmed in."""
        height = int(self.node.call("getblockcount")) - int(lot["confirmations"]) + 1
        prev = self.node.call("getblockheader", self.node.call("getblockhash", height - 1))
        return int(prev["mediantime"])


# --- the service -----------------------------------------------------------

def _levod_terms(base):
    cache = {}

    def terms_of(slug):
        hit = cache.get(slug)
        if hit and hit[0] > time.time():
            return hit[1]
        with urllib.request.urlopen("%s/api/projects/%s" % (base, slug), timeout=20) as r:
            d = json.loads(r.read())
        sale = (d.get("project") or d).get("sale") or {}
        terms = C.SaleTerms.from_json(sale.get("terms"))
        cache[slug] = (time.time() + 60, terms)
        return terms
    return terms_of


def make_handler(rcv):
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
                    return self._send(200, {"ok": True, "receiver_xonly": rcv.xonly})
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
                                                       b["refund_spk"]))
                if self.path == "/reserve-signature":
                    return self._send(200, rcv.sign_reserve(b["payment_hash"], b["sale_coin"],
                                                            b["inputs"], b["change_spk"],
                                                            b["fee_atoms"]))
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
    ver, prog = ADDR.witness_program(env["LEVO_RECEIVER_BOND_TO"], hrp)
    bond_to = (bytes([0x51 if ver == 1 else 0x00, len(bytes.fromhex(prog))]) + bytes.fromhex(prog)).hex()
    slugs = [s.strip() for s in env.get("LEVO_RECEIVER_SLUGS", "").split(",") if s.strip()] or None
    fee = env.get("LEVO_RECEIVER_FEE_ATOMS")
    rcv = Receiver(secret, Cln(env["LEVO_RECEIVER_LIGHTNING"]), node,
                   _levod_terms(env["LEVO_RECEIVER_LEVO"].rstrip("/")),
                   env.get("LEVO_RECEIVER_STATE", "receiver-state.json"), bond_to, slugs,
                   int(env.get("LEVO_RECEIVER_INVOICE_EXPIRY", "600")),
                   int(fee) if fee else None)

    def loop():
        while True:
            try:
                for what in rcv.poll():
                    sys.stderr.write("receiver: %s %s %s\n" % what)
            except Exception as e:
                sys.stderr.write("receiver: poll failed: %s\n" % e)
            time.sleep(int(env.get("LEVO_RECEIVER_POLL", "10")))
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((env.get("LEVO_RECEIVER_BIND", "127.0.0.1"),
                               int(env.get("LEVO_RECEIVER_PORT", "8098"))), make_handler(rcv))
    sys.stderr.write("receiver: serving key %s\n" % rcv.xonly)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
