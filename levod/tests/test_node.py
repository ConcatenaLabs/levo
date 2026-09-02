#!/usr/bin/env python3
"""Levo against a real Sequentia node.

Everything else in the gate stubs the chain. This starts an actual sequentiad
on a throwaway regtest chain, issues two assets, and runs the platform's own
code through the whole life of a sale: lock, a purchase as a browser wallet
would make it (a PSET the node's wallet signs and finalises), a purchase as a
node makes it (raw hex), the watcher moving the sale from a remainder still in
the mempool, a sell-out, a reclaim, and the spends the covenant must refuse.
If the node accepts what Levo builds and refuses what it should, the covenant,
the transaction builder and the watcher are right; nothing else proves that.

It looks for the node at $SEQUENTIAD, then $SEQUENTIA_SRC/src/sequentiad, then
~/Sequentia/src/sequentiad, and reports itself skipped when there is none.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import covenant as C  # noqa: E402
import market as M  # noqa: E402
import pset as P  # noqa: E402
import rpc as RPCMOD  # noqa: E402
import sale as S  # noqa: E402
import secp256k1 as K  # noqa: E402
import store as ST  # noqa: E402
import tiers as T  # noqa: E402
import tx as TX  # noqa: E402
import watcher as W  # noqa: E402

COIN = 100_000_000


def find_node():
    cands = [os.environ.get("SEQUENTIAD")]
    src = os.environ.get("SEQUENTIA_SRC") or os.path.expanduser("~/Sequentia")
    cands += [os.path.join(src, "src", "sequentiad"), os.path.join(src, "src", "elementsd")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Wallet:
    """The node's wallet RPC, positional or named. Test-only: levod never
    makes a wallet call."""

    def __init__(self, url, user, pw, wallet=None):
        self.url = url + ("/wallet/" + wallet if wallet else "")
        self.auth = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
        self.i = 0

    def __call__(self, method, *params, **named):
        self.i += 1
        req = urllib.request.Request(
            self.url, data=json.dumps({"jsonrpc": "1.0", "id": self.i, "method": method,
                                       "params": named if named else list(params)}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Basic " + self.auth})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                p = json.loads(r.read(), parse_float=Decimal)
        except urllib.error.HTTPError as e:
            p = json.loads(e.read(), parse_float=Decimal)
        if p.get("error"):
            raise RuntimeError("%s: %s" % (method, p["error"].get("message", p["error"])))
        return p["result"]


class Rig:
    def __init__(self, binary):
        self.root = tempfile.mkdtemp(prefix="levo-node-")
        self.port = free_port()
        args = [binary, "-datadir=%s" % self.root, "-chain=elementsregtest",
                "-initialfreecoins=2100000000000000", "-con_blocksubsidy=0",
                "-con_connect_genesis_outputs=1", "-con_any_asset_fees=1",
                "-anyonecanspendaremine=1", "-blindedaddresses=0",
                # Sequentia is transparent by default. A custom chain keeps the
                # Elements default unless told, and then the wallet blinds its
                # change even though its addresses are unblinded.
                "-con_default_blinded_addresses=0", "-validatepegin=0",
                "-con_parent_chain_signblockscript=51", "-txindex=0",
                "-fallbackfee=0.0002", "-listen=0", "-server=1", "-persistmempool=0",
                "-rpcport=%d" % self.port, "-rpcuser=levo", "-rpcpassword=levo", "-daemon=0"]
        self.log = open(os.path.join(self.root, "stdout.log"), "w")
        self.proc = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT)
        self.url = "http://127.0.0.1:%d" % self.port
        self.n = Wallet(self.url, "levo", "levo")
        for _ in range(240):
            try:
                self.n("getblockchaininfo")
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("sequentiad exited; see %s/stdout.log" % self.root)
                time.sleep(0.25)
        self.n("createwallet", "levo")
        self.w = Wallet(self.url, "levo", "levo", "levo")
        self.w("rescanblockchain", 0)
        self.mine(101)

    def mine(self, k=1):
        self.w("generatetoaddress", k, self.w("getnewaddress"))

    def stop(self):
        try:
            self.n("stop")
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.root, ignore_errors=True)


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, cond, what, detail=""):
        if cond:
            self.passed += 1
        else:
            self.failed.append("%s%s" % (what, (" (%s)" % detail) if detail else ""))

    def eq(self, got, want, what):
        self.ok(got == want, what, "got %r, want %r" % (got, want))

    def report(self):
        for f in self.failed:
            print("  FAIL %s" % f)
        print("%d passed, %d failed" % (self.passed, len(self.failed)))
        return 0 if not self.failed else 1


class Founder:
    """Listing needs a tier; a fresh regtest has no stake. The tier gate has
    its own tests, so here the issuer is simply allowed to list."""

    def __init__(self):
        self.links = T.StakeLinks()
        self.policy = T.TierPolicy()

    def standing(self, a):
        return {"tier": self.policy.tiers[-1].to_json(), "stake_atoms": 10**18, "stake": 1e10}


def run(ok, rig):
    w, n = rig.w, rig.n
    node = RPCMOD.NodeRPC(url=rig.url, user="levo", password="levo")
    token = w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    pay = w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    rig.mine()
    n("setfeeexchangerates", {"bitcoin": COIN, pay: COIN})

    # --- the platform, on the real node ----------------------------------
    plat = M.Platform(ST.Store(os.path.join(rig.root, "levo-state.json")), Founder(), None, node,
                      hrp="ert", payment_asset=pay, payment_label="PAY")
    watch = W.Watcher(plat, node, hrp="ert")
    real_reader = T.StakeReader(node, T.StakeLinks())
    st = real_reader.standing("02" + "aa" * 32)
    ok.eq(st["staking_available"], False, "a chain without proof of stake reports no staking, not an error")
    ok.eq(st["tier"]["name"], "Visitor", "and every account is a visitor")

    issuer = "02" + "11" * 32
    tier = plat.stake.policy.tiers[-1]
    reclaim_sec = 0x7777777777777777777777777777777777777777777777777777777777777777
    treasury_addr = w("getnewaddress", "", "bech32")     # v0: the treasury must be taproot
    tap_sec = 0x5555555555555555555555555555555555555555555555555555555555555555
    treasury_prog = K.xonly_pubkey(tap_sec).hex()
    fee_for = {"bitcoin": "bitcoin"}

    def terms(close, total=10_000):
        return {"token_asset": token, "payment_asset": pay, "price_num": 25, "price_den": 100,
                "treasury_prog": treasury_prog, "min_lot": 100 * COIN, "close_locktime": close,
                "reclaim_xonly": K.xonly_pubkey(reclaim_sec).hex(), "total_atoms": total * COIN}

    def fund(slug):
        p = plat.projects[slug]
        addr = plat.sale_address(p.sale)
        txid = w("sendtoaddress", address=addr, amount=p.sale.terms.total_atoms // COIN,
                 assetlabel=token, fee_asset_label="bitcoin")
        return p, txid

    def pay_input(min_atoms, retry=True):
        pay_outs = [u for u in w("listunspent", 0) if u["asset"] == pay]
        for u in pay_outs:
            if RPCMOD.to_atoms(u["amount"]) >= min_atoms and not u.get("amountcommitment") \
                    and not u.get("assetcommitment"):
                return plat.verify_buyer_inputs([{"txid": u["txid"], "vout": u["vout"]}])
        if retry and pay_outs:
            # The wallet blinds the change of an ordinary send, so after one
            # the only large payment output can be confidential -- which is
            # what a buyer runs into, and what the copy tells them to do about
            # it: move the balance to an unblinded address first.
            total = sum(RPCMOD.to_atoms(u["amount"]) for u in pay_outs)
            w("sendtoaddress", address=w("getnewaddress"), amount="%.8f" % (total / COIN),
              assetlabel=pay, fee_asset_label="bitcoin")
            rig.mine()
            return pay_input(min_atoms, retry=False)
        raise RuntimeError("no explicit payment output of %d atoms; wallet holds %s" % (
            min_atoms, [(str(u["amount"]), bool(u.get("amountcommitment")), u.get("confirmations")) for u in pay_outs]))

    def accept(hexed):
        r = w("testmempoolaccept", [hexed])[0]
        return r.get("allowed"), r.get("reject-reason")

    def dest_spk():
        return w("getaddressinfo", w("getnewaddress"))["scriptPubKey"]

    # --- listing refuses a v0 treasury address and a spent-asset mistake --
    try:
        plat.list_project(issuer, {"slug": "bad", "name": "Bad", "ticker": "BAD"},
                          dict(terms(node.chain_height() + 500), treasury_address=treasury_addr))
        ok.ok(False, "a v0 treasury address is refused")
    except M.PlatformError as e:
        ok.ok("taproot" in str(e), "a v0 treasury address is refused with the reason", str(e))

    # --- alpha: lock found by scan, PSET buy, remainder seen in the mempool -
    h = node.chain_height()
    plat.list_project(issuer, {"slug": "alpha", "name": "Alpha", "ticker": "ALP",
                               "summary": "s", "description": "d"}, terms(h + 500))
    A, lock_txid = fund("alpha")
    try:
        plat.confirm_lock(issuer, "alpha")
        ok.ok(False, "an unconfirmed lock is not found by the scan")
    except M.PlatformError as e:
        ok.ok("confirmed" in str(e), "an unconfirmed lock is not found by the scan, and the reason says so")
    rig.mine()
    plat.confirm_lock(issuer, "alpha")
    ok.eq(A.sale.status, S.LIVE, "the lock is found on chain by scanning, without naming the outpoint")
    ok.eq(A.sale.funding["txid"], lock_txid, "at the funding transaction")
    watch.poll()
    ok.ok(A.sale.funding.get("block"), "the watcher notes the block the lock confirmed in")
    ok.eq(plat.verify_buyer_inputs([]), [], "no inputs is an empty list")

    # A browser wallet's purchase: PSET signed and finalised by the wallet.
    plan = A.sale.plan_buy("buyer", tier, token_atoms=1_000 * COIN, height=node.chain_height())
    ins = pay_input(plan.payment_atoms + 5_000)
    built = plat.build_buy("buyer", "alpha", {"token_atoms": 1_000 * COIN},
                           {"token_script_pubkey": dest_spk(), "change_script_pubkey": dest_spk(),
                            "inputs": ins, "fee_atoms": 1_000, "fee_asset": pay})
    ok.ok(built["pset"], "a PSET comes with the transaction")
    ok.eq(built["fee"]["asset"], pay, "and fee advice in the payment asset")
    dec = n("decodepsbt", built["pset"])
    ok.eq(len(dec["inputs"]), 2, "the node decodes the PSET")
    ok.ok("final_scriptwitness" in dec["inputs"][0], "with the covenant's witness already final")
    proc = w("walletprocesspsbt", built["pset"])
    ok.eq(proc["complete"], True, "the wallet signs its own input")
    fin = w("finalizepsbt", proc["psbt"])
    ok.eq(fin["complete"], True, "and finalises")
    allowed, why = accept(fin["hex"])
    ok.ok(allowed, "the node accepts the PSET purchase", why)
    txid = w("sendrawtransaction", fin["hex"])
    ok.eq(txid, built["txid"], "the txid is the one Levo computed before signing")
    plat.record_purchase("buyer", "alpha", txid, 1_000 * COIN, plan.payment_atoms)
    # Before any block: the remainder is only in the mempool. The recorded
    # purchase told the watcher where to look.
    r = watch.poll()
    ok.eq(A.sale.status, S.PARTIAL, "the sale moves to its remainder while the buy is still in the mempool")
    ok.eq(A.sale.funding["txid"], txid, "at the purchase's output 1")
    ok.eq(A.sale.locked_atoms, 9_000 * COIN, "holding what is left")
    rig.mine()
    watch.poll()
    ok.ok(A.sale.funding.get("block"), "and notes its block once confirmed")
    # A second purchase against the old outpoint is refused before anyone signs.
    try:
        plat.build_buy("buyer", "alpha", {"token_atoms": 100 * COIN},
                       {"token_script_pubkey": dest_spk(), "inputs": ins, "fee_atoms": 1_000})
        ok.ok(False, "a spent input is refused")
    except M.PlatformError as e:
        ok.ok("not an unspent output" in str(e), "a spent input is refused before signing")

    # --- the covenant refuses what it must ---------------------------------
    def raw_buy(sale, token_atoms, mutate=None, fee=1_000, late=False):
        if late:
            # After the close Levo plans nothing; the chain still would. Build
            # the plan by hand to show the sell leaf carries no locktime.
            plan = S.BuyPlan(sale, "x", token_atoms, sale.terms.cost_for(token_atoms),
                             sale.locked_atoms - token_atoms)
        else:
            plan = sale.plan_buy("x", tier, token_atoms=token_atoms, height=node.chain_height())
        ins2 = pay_input(plan.payment_atoms + fee)
        b = TX.build_buy(sale, plan, {"token_script_pubkey": dest_spk(), "change_script_pubkey": dest_spk(),
                                      "inputs": ins2, "fee_atoms": fee, "fee_asset": pay})
        hexed = b["unsigned_tx_hex"]
        if mutate:
            t = TX.Transaction()
            t.vin.append(TX.TxIn(sale.funding["txid"], sale.funding["vout"], witness=sale.cov.sell_witness()))
            for i in ins2:
                t.vin.append(TX.TxIn(i["txid"], i["vout"]))
            for o in b["outputs"]:
                t.vout.append(TX.TxOut(o["asset"], o["atoms"], o["script_pubkey"]))
            mutate(t)
            hexed = t.hex()
        signed = w("signrawtransactionwithwallet", hexed)
        return accept(signed["hex"]), signed["hex"]

    def underpay(t):
        t.vout[0].atoms -= 1
        t.vout[3].atoms += 1                      # the buyer's change grows by the same atom

    (allowed, why), _ = raw_buy(A.sale, 1_000 * COIN, underpay)
    ok.ok(not allowed, "paying one atom below the price is refused", why)

    def tokens_at_one(t):
        # Put the buyer's tokens where the remainder goes: the leaf reads them
        # as a remainder that failed to return to the covenant.
        t.vout[1], t.vout[2] = t.vout[2], t.vout[1]

    (allowed, why), _ = raw_buy(A.sale, 1_000 * COIN, tokens_at_one)
    ok.ok(not allowed, "the buyer's tokens at output 1 are refused", why)

    def dust_remainder(t):
        t.vout[1].atoms -= 50 * COIN              # leave a remainder below min_lot
        t.vout[2].atoms += 50 * COIN

    (allowed, why), _ = raw_buy(A.sale, 1_000 * COIN, dust_remainder)
    ok.ok(not allowed, "a remainder below the minimum lot is refused", why)

    try:
        A.sale.plan_buy("x", tier, token_atoms=A.sale.locked_atoms - 50 * COIN, height=node.chain_height())
        ok.ok(False, "Levo refuses to plan a dust remainder")
    except S.SaleError as e:
        ok.ok("clear the sale" in str(e), "Levo refuses to plan a dust remainder, and says how to clear it")

    (allowed, why), hexed = raw_buy(A.sale, 1_000 * COIN)
    ok.ok(allowed, "the unmodified purchase is accepted", why)

    # --- selling out, by a purchase Levo never saw --------------------------
    (allowed, why), hexed = raw_buy(A.sale, A.sale.locked_atoms)
    ok.ok(allowed, "a full buy with change at output 1 is accepted", why)
    w("sendrawtransaction", hexed)
    watch.poll()
    rig.mine()
    watch.poll()
    if A.sale.status != S.SOLD_OUT:
        rig.mine()
        watch.poll()
    ok.eq(A.sale.status, S.SOLD_OUT, "a sale bought out without Levo reads as sold out, not as a reorg")
    ok.eq(A.sale.locked_atoms, 0, "holding nothing")
    watch.poll(); rig.mine(); watch.poll()
    ok.eq(A.sale.status, S.SOLD_OUT, "and stays sold out")

    # --- beta: a height close, a late sell, and the reclaim -----------------
    h = node.chain_height()
    plat.list_project(issuer, {"slug": "beta", "name": "Beta", "ticker": "BET",
                               "summary": "s", "description": "d"}, terms(h + 6))
    B, _ = fund("beta")
    rig.mine()
    plat.confirm_lock(issuer, "beta")
    watch.poll()
    (allowed, why), hexed = raw_buy(B.sale, 500 * COIN)
    ok.ok(allowed, "a partial buy on beta", why)
    w("sendrawtransaction", hexed)
    rig.mine()
    watch.poll()
    ok.eq(B.sale.status, S.PARTIAL, "beta is partial")
    try:
        plat.build_reclaim(issuer, "beta", {"destination_script_pubkey": dest_spk(),
                                            "fee_inputs": [], "fee_atoms": 0})
        ok.ok(False, "a reclaim before the close is refused")
    except M.PlatformError as e:
        ok.ok("not closed" in str(e), "a reclaim before the close is refused by Levo")
    rig.mine(8)
    watch.poll()
    ok.ok(B.sale.has_closed(height=node.chain_height()), "past its close")
    ok.eq(B.sale.shown_status(height=node.chain_height()), S.CLOSED, "shown as closed")
    # The sell leaf carries no locktime: a late buy is still valid on chain.
    (allowed, why), _ = raw_buy(B.sale, 100 * COIN, late=True)
    ok.ok(allowed, "the chain still accepts a buy after the close: only the reclaim path opened", why)
    # A reclaim with the wrong key is refused by the chain; the right one lands.
    fee_ins = pay_input(5_000)
    r = plat.build_reclaim(issuer, "beta", {"destination_script_pubkey": dest_spk(),
                                            "fee_inputs": [{"txid": i["txid"], "vout": i["vout"]} for i in fee_ins],
                                            "fee_atoms": 1_000})
    ok.ok("signature" not in r, "levod hands back a sighash, never a signature")
    wrong = K.schnorr_sign(bytes.fromhex(r["sighash"]), reclaim_sec + 1)
    ok.ok(not TX.check_reclaim_signature(B.sale, r["sighash"], wrong), "a wrong key is caught before relay")
    sig = K.schnorr_sign(bytes.fromhex(r["sighash"]), reclaim_sec)
    signed = w("signrawtransactionwithwallet", r["unsigned_tx_hex"])
    allowed, why = accept(TX.set_witness(signed["hex"], 0, [wrong] + TX.reclaim_witness(B.sale, sig)[1:]))
    ok.ok(not allowed, "the chain refuses a reclaim signed by the wrong key", why)
    good = TX.set_witness(signed["hex"], 0, TX.reclaim_witness(B.sale, sig))
    allowed, why = accept(good)
    ok.ok(allowed, "the chain accepts the reclaim after the close", why)
    rtx = w("sendrawtransaction", good)
    ok.eq(rtx, r["txid"], "with the txid Levo noted")
    rig.mine()
    watch.poll(); rig.mine(); watch.poll()
    ok.eq(B.sale.status, S.RECLAIMED, "the watcher proves the reclaim by its output and says so")
    ok.eq(B.sale.locked_atoms, 0, "the covenant is empty")

    # --- gamma: a stray asset at the sale address is reported ---------------
    h = node.chain_height()
    plat.list_project(issuer, {"slug": "gamma", "name": "Gamma", "ticker": "GAM",
                               "summary": "s", "description": "d"}, terms(h + 500))
    G, _ = fund("gamma")
    rig.mine()
    plat.confirm_lock(issuer, "gamma")
    w("sendtoaddress", address=plat.sale_address(G.sale), amount=7, assetlabel=pay, fee_asset_label="bitcoin")
    rig.mine()
    # The known outpoint answers, so no scan runs; force one by asking after
    # the outpoint moved. Simplest: a buy, then a poll before it confirms.
    (allowed, why), hexed = raw_buy(G.sale, 100 * COIN)
    w("sendrawtransaction", hexed)
    rig.mine()
    watch.poll()
    ok.eq(G.sale.status, S.PARTIAL, "gamma sold a lot")
    ok.eq([(s["asset"], s["atoms"]) for s in G.sale.strays], [(pay, 7 * COIN)],
          "the stray payment asset resting at the sale address is reported")
    # A stray reclaim-before-close of the wrong kind is refused by the builder.
    try:
        TX.build_reclaim(G.sale, dest_spk(), [], 0, pay, n("getblockhash", 0), locktime=1_800_000_000)
        ok.ok(False, "a time locktime on a height-closed sale is refused")
    except TX.BuildError:
        ok.ok(True, "a time locktime on a height-closed sale is refused")


def main():
    binary = find_node()
    if not binary:
        print("skipped: no sequentiad found (set SEQUENTIAD or SEQUENTIA_SRC)")
        return 0
    ok = Checker()
    rig = Rig(binary)
    try:
        run(ok, rig)
    except Exception:
        import traceback
        ok.failed.append("the drill raised:\n" + traceback.format_exc())
    finally:
        rig.stop()
    return ok.report()


if __name__ == "__main__":
    sys.exit(main())
