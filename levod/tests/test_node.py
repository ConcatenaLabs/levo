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
import copy
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

import address as ADDR  # noqa: E402
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
        self.binary = binary
        self.root = tempfile.mkdtemp(prefix="levo-node-")
        self.port = free_port()
        self.extra = []
        self._launch()
        self.n("createwallet", "levo")
        self.w = Wallet(self.url, "levo", "levo", "levo")
        self.w("rescanblockchain", 0)
        self.mine(101)

    def _launch(self):
        binary = self.binary
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
                "-rpcport=%d" % self.port, "-rpcuser=levo", "-rpcpassword=levo",
                "-daemon=0"] + list(self.extra)
        self.log = open(os.path.join(self.root, "stdout.log"), "a")
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

    def restart(self, extra=()):
        """Stop and start the node again, keeping the chain and the wallet.

        The rig runs with -persistmempool=0, so a restart is also the way to
        make a transaction that was only ever in the mempool really go away --
        which is the one thing that makes a funding a ghost rather than a spend
        nobody has seen yet.
        """
        try:
            self.n("stop")
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()
        self.log.close()
        self.extra = list(extra)
        self._launch()
        self.w = Wallet(self.url, "levo", "levo", "levo")
        try:
            self.n("loadwallet", "levo")
        except Exception:
            pass

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
    # Regtest mines a block the instant it is asked to, so the tip must never
    # be reused here: a held tip is correct against a chain with block times,
    # and wrong against one without.
    node.chain_info_ttl = 0
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

    # --- listing refuses what the chain would not pay ---------------------
    try:
        plat.list_project(issuer, {"slug": "bad", "name": "Bad", "ticker": "BAD"},
                          dict(terms(node.chain_height() + 500),
                               treasury_address=ADDR.from_script_pubkey("5220" + "aa" * 32, "ert")))
        ok.ok(False, "an address the chain treats as anyone-can-spend is refused as a treasury")
    except M.PlatformError as e:
        ok.ok("treasury" in str(e).lower() or "address" in str(e).lower(),
              "an unusable treasury address is refused with a reason", str(e))

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
    ok.eq(A.sale.funding.get("height"), node.chain_height(),
          "the watcher notes the height the lock confirmed at")
    ok.eq(A.sale.funding.get("block"), n("getblockhash", node.chain_height()),
          "and the block at that height, which is what a reorg changes")
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

    # --- the advertised minimum fee is one the node will actually relay ----
    #
    # Levo tells a buyer the smallest fee this node relays for a transaction of
    # this shape. If that figure is short, the buyer signs a transaction that
    # simply never confirms, and nothing says why.
    advice = plat.fee_advice(A.sale, n_inputs=1)
    ok.ok(advice["min_atoms"] and advice["min_atoms"] > 0, "the node quotes a floor")
    min_ins = pay_input(A.sale.terms.cost_for(100 * COIN) + advice["min_atoms"])
    at_floor = plat.build_buy("buyer", "alpha", {"token_atoms": 100 * COIN},
                              {"token_script_pubkey": dest_spk(),
                               "change_script_pubkey": dest_spk(),
                               "inputs": min_ins, "fee_atoms": advice["min_atoms"],
                               "fee_asset": pay})
    ok.ok(at_floor["vsize_estimate"] <= advice["vsize_estimate"],
          "and sizes the transaction at or above what it really is",
          "%s vs %s" % (at_floor["vsize_estimate"], advice["vsize_estimate"]))
    at_floor_signed = w("signrawtransactionwithwallet", at_floor["unsigned_tx_hex"])
    allowed, why = accept(at_floor_signed["hex"])
    ok.ok(allowed, "a purchase paying exactly the advertised minimum is relayed", why)
    try:
        plat.build_buy("buyer", "alpha", {"token_atoms": 100 * COIN},
                       {"token_script_pubkey": dest_spk(), "change_script_pubkey": dest_spk(),
                        "inputs": min_ins, "fee_atoms": 1, "fee_asset": pay})
        ok.ok(False, "a fee below the floor is refused")
    except M.PlatformError as e:
        ok.ok("relay" in str(e), "and a fee below that floor is refused before signing", str(e))

    # --- the dust floor Levo enforces is the one the node enforces ---------
    #
    # A node drops a transaction carrying an output too small to be worth
    # spending. The rate behind that rule is compiled into the node and is not
    # reported over RPC, so Levo carries its own copy -- and a copy that has
    # drifted from the node is worse than none: it refuses purchases the chain
    # would take, or builds ones it will not. This walks the boundary against
    # the node itself.
    floor = plat.dust_atoms(pay, spk_len=len(dest_spk()) // 2)
    ok.ok(floor and floor > 0, "levod prices the dust floor for the payment asset")
    for atoms, want in ((floor - 1, False), (floor, True)):
        # Either end of the node can refuse it -- its wallet will not fund a
        # dust output, and its mempool will not relay one -- and both apply
        # the same rule. What is being pinned is the boundary, not which half
        # of the node says no.
        try:
            funded = w("fundrawtransaction", w("createrawtransaction", [], [
                {w("getnewaddress"): "%d.%08d" % divmod(atoms, 100_000_000),
                 "asset": pay}]), {"fee_rate": 1, "fee_asset": pay})
            signed = w("signrawtransactionwithwallet", funded["hex"])
            allowed, why = accept(signed["hex"])
        except Exception as e:
            allowed, why = False, str(e)
        ok.ok(bool(allowed) == want,
              "an output of %d atoms is %s by the node"
              % (atoms, "relayed" if want else "refused"), why)

    # --- a sale whose treasury is an ordinary version-0 address ------------
    #
    # Most wallets, the browser extension included, hand out version-0
    # addresses and no taproot one. A sale whose treasury is one has to work
    # exactly as well, or an issuer using such a wallet cannot list at all.
    h = node.chain_height()
    v0_terms = terms(h + 500)
    v0_terms.pop("treasury_prog")
    v0_terms["treasury_address"] = treasury_addr
    plat.list_project(issuer, {"slug": "vzero", "name": "V Zero", "ticker": "VZ",
                               "summary": "s", "description": "d"}, v0_terms)
    V = plat.projects["vzero"]
    ok.eq(V.sale.terms.treasury_ver, 0, "the version comes from the address given")
    ok.eq(V.sale.terms.treasury_spk.hex(), w("getaddressinfo", treasury_addr)["scriptPubKey"],
          "and the credit the leaf checks is that address's own script")
    fund("vzero")
    rig.mine()
    plat.confirm_lock(issuer, "vzero")
    ok.eq(V.sale.status, S.LIVE, "a version-0 treasury sale funds like any other")
    vplan = V.sale.plan_buy("buyer", tier, token_atoms=1_000 * COIN, height=node.chain_height())
    vins = pay_input(vplan.payment_atoms + 5_000)
    vbuilt = plat.build_buy("buyer", "vzero", {"token_atoms": 1_000 * COIN},
                            {"token_script_pubkey": dest_spk(), "change_script_pubkey": dest_spk(),
                             "inputs": vins, "fee_atoms": 1_000, "fee_asset": pay})
    vsigned = w("signrawtransactionwithwallet", vbuilt["unsigned_tx_hex"])
    allowed, why = accept(vsigned["hex"])
    ok.ok(allowed, "and the chain accepts a purchase that pays a version-0 treasury", why)
    vtxid = w("sendrawtransaction", vsigned["hex"])
    rig.mine()
    watch.poll()
    ok.eq(V.sale.status, S.PARTIAL, "the watcher follows it like any other sale")
    ok.eq(V.sale.sold_atoms, 1_000 * COIN, "with what was sold")
    paid = next(o for o in n("decoderawtransaction", vsigned["hex"])["vout"]
                if o.get("scriptPubKey", {}).get("hex") == V.sale.terms.treasury_spk.hex())
    ok.eq(RPCMOD.to_atoms(paid["value"]), vplan.payment_atoms,
          "and the treasury was paid at its own address")

    # --- a funding that never reached a block is a ghost --------------------
    #
    # The dangerous mistake is the opposite one: calling a sale that sold out a
    # ghost, and voiding its buyers' allocations. So this proves the ghost path
    # on the one case that really is a ghost -- a lock confirmed from the
    # mempool that then never lands -- and the sold-out cases above prove it
    # does not fire when the funding is on the chain.
    h = node.chain_height()
    plat.list_project(issuer, {"slug": "ghosted", "name": "Ghosted", "ticker": "GHO",
                               "summary": "s", "description": "d"},
                      terms(h + 500, total=5_000))
    G, gtxid = fund("ghosted")
    graw = n("decoderawtransaction", w("gettransaction", gtxid)["hex"])
    gvout = next(o["n"] for o in graw["vout"]
                 if o.get("scriptPubKey", {}).get("hex") == G.sale.script_pubkey)
    plat.confirm_lock(issuer, "ghosted", gtxid, gvout)
    ok.eq(G.sale.status, S.LIVE, "a lock still in the mempool can be confirmed by its outpoint")
    ok.ok(G.sale.funding.get("seen_height"), "and is dated by the height it was first seen at")
    ok.ok(not G.sale.funding.get("block"), "with no block, because it has none")
    watch.poll()
    ok.eq(G.sale.status, S.LIVE, "the watcher leaves an unconfirmed lock alone")
    # The rig keeps no mempool across a restart, and the wallet is told not to
    # rebroadcast: the funding transaction is now in no block and no mempool,
    # which is the whole of what makes a sale a ghost.
    rig.restart(["-walletbroadcast=0"])
    ok.eq(w("getrawmempool"), [], "after the restart the mempool is empty")
    watch.poll()
    rig.mine()
    watch.poll()
    ok.eq(G.sale.status, S.GHOST, "a funding in no block and no mempool ghosts the sale")
    ok.eq(G.sale.funding, None, "and the sale holds no outpoint")
    ok.eq(A.sale.status, S.PARTIAL, "while the funded sale beside it is untouched")
    ok.eq(V.sale.status, S.PARTIAL, "and so is the version-0 one")
    # And it comes back, at the same address, when the tokens are really sent.
    # The wallet still holds the dropped transaction and counts its inputs as
    # spent, so it is abandoned first -- the same step a project would take.
    w("abandontransaction", gtxid)
    rig.restart()                      # broadcasting again, like any ordinary node
    fund("ghosted")
    rig.mine()
    plat.confirm_lock(issuer, "ghosted")
    watch.poll()
    ok.eq(G.sale.status, S.LIVE, "locking again reopens the sale")
    ok.ok(G.sale.funding.get("block"), "now with the block it was mined in")

    # --- a reorg under a funded sale ---------------------------------------
    #
    # Sequentia follows Bitcoin's reorgs in real time, so a block being replaced
    # is an ordinary event here rather than an exotic one, and the README says a
    # lock undone by one stops being investable. Everything above proves the
    # ghost path on a lock that never confirmed; this proves it on one that DID,
    # and proves the ordinary case first: a reorg that unconfirms a funding and
    # then mines it again must leave the sale exactly as it was.
    h = node.chain_height()
    plat.list_project(issuer, {"slug": "reorged", "name": "Reorged", "ticker": "RRG",
                               "summary": "s", "description": "d"},
                      terms(h + 500, total=5_000))
    R, rtxid = fund("reorged")
    rig.mine()
    plat.confirm_lock(issuer, "reorged")
    watch.poll()
    ok.eq(R.sale.status, S.LIVE, "the reorg sale is funded and live")
    ok.ok(R.sale.funding.get("block"), "with the block it was mined in")
    at = R.sale.funding["height"]
    block = n("getblockhash", at)

    n("invalidateblock", block)
    watch.poll()
    ok.eq(R.sale.status, S.LIVE,
          "a reorg that unconfirms the funding leaves the sale alone: the "
          "transaction is in the mempool, and the covenant still holds it")
    ok.eq(R.sale.locked_atoms, 5_000 * COIN, "holding exactly what it held")

    n("reconsiderblock", block)
    watch.poll()
    ok.eq(R.sale.status, S.LIVE, "and it is still live when the block comes back")
    ok.ok(R.sale.funding.get("block"), "with a block noted again")

    # Now the reorg that really takes it: the block is replaced and the
    # transaction is never mined again, which on a chain that follows Bitcoin
    # is what a lock undone by an anchor-driven reorg looks like.
    n("invalidateblock", n("getblockhash", R.sale.funding["height"]))
    rig.restart(["-walletbroadcast=0"])          # the mempool does not survive it
    ok.eq(w("getrawmempool"), [], "the funding is in no mempool")
    rig.mine(2)
    for _ in range(3):
        watch.poll()
        rig.mine()          # the miss protocol wants a block between two looks
    ok.eq(R.sale.status, S.GHOST,
          "a funding whose block was reorged away and never mined again ghosts "
          "the sale")
    ok.eq(R.sale.funding, None, "and the sale holds no outpoint")
    ok.eq(A.sale.status, S.PARTIAL, "with the sale beside it untouched")
    w("abandontransaction", rtxid)
    rig.restart()

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
    ok.ok(not allowed, "a remainder below the minimum purchase is refused", why)

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
                                            "change_script_pubkey": dest_spk(),
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

    # --- a sale that closes at a TIME rather than a height ------------------
    #
    # A locktime below 500,000,000 is a height and above it a unix time, and
    # the chain judges a time against median time past rather than the wall
    # clock. Both paths compile into the reclaim leaf, so both need proving on
    # a real node: a reclaim built against the wrong clock is rejected as
    # non-final after the project has signed it.
    now = int(time.time())
    close_at = now + 3600
    plat.list_project(issuer, {"slug": "timed", "name": "Timed", "ticker": "TMD",
                               "summary": "s", "description": "d"},
                      terms(close_at, total=2_000))
    TS = plat.projects["timed"]
    fund("timed")
    rig.mine()
    plat.confirm_lock(issuer, "timed")
    watch.poll()
    ok.eq(TS.sale.close_is_height(), False, "the sale closes at a time, not a height")
    ok.eq(TS.sale.status, S.LIVE, "and is live before it")
    try:
        plat.build_reclaim(issuer, "timed", {"destination_script_pubkey": dest_spk(),
                                             "fee_inputs": [], "fee_atoms": 1_000})
        ok.ok(False, "a reclaim before a time close is refused")
    except M.PlatformError as e:
        ok.ok("clock" in str(e), "a reclaim before a time close is refused by the chain's clock", str(e))
    # Move the chain's clock past the close. Median time past is the median of
    # the last eleven blocks, so a few blocks at the new time carry it over.
    n("setmocktime", close_at + 600)
    rig.mine(12)
    ok.ok(node.median_time() > close_at, "the chain's median time is past the close")
    ok.eq(TS.sale.shown_status(now=node.median_time()), S.CLOSED, "so the sale reads closed")
    t_fee_ins = pay_input(5_000)
    tr = plat.build_reclaim(issuer, "timed", {
        "destination_script_pubkey": dest_spk(),
        "change_script_pubkey": dest_spk(),
        "fee_inputs": [{"txid": i["txid"], "vout": i["vout"]} for i in t_fee_ins],
        "fee_atoms": 1_000})
    ok.eq(tr["locktime"], close_at, "the reclaim carries the sale's own close as its locktime")
    tsig = K.schnorr_sign(bytes.fromhex(tr["sighash"]), reclaim_sec)
    tsigned = w("signrawtransactionwithwallet", tr["unsigned_tx_hex"])
    allowed, why = accept(TX.set_witness(tsigned["hex"], 0, TX.reclaim_witness(TS.sale, tsig)))
    ok.ok(allowed, "and the chain accepts a time-locked reclaim once its clock has passed", why)
    n("setmocktime", 0)

    # --- a sale that moves twice between two polls -------------------------
    #
    # The watcher can miss a sale's whole middle: a recorded buy leaves a
    # remainder, and a second buy takes it, both before the next poll. Nothing
    # rests, and the outpoint the watcher knew is long gone. What must NOT
    # happen is a ghost -- that would tell buyers a sale they were paid from
    # was never funded, and wipe their allocations.
    h = node.chain_height()
    plat.list_project(issuer, {"slug": "swift", "name": "Swift", "ticker": "SWF",
                               "summary": "s", "description": "d"},
                      terms(h + 500, total=4_000))
    SW = plat.projects["swift"]
    fund("swift")
    rig.mine()
    plat.confirm_lock(issuer, "swift")
    watch.poll()
    ok.eq(SW.sale.status, S.LIVE, "swift is live")
    first_buy = 1_000 * COIN
    (allowed, why), hexed = raw_buy(SW.sale, first_buy)
    ok.ok(allowed, "the first buy is valid", why)
    tx1 = w("sendrawtransaction", hexed)
    plat.record_purchase("buyer", "swift", tx1, first_buy, SW.sale.terms.cost_for(first_buy))
    # The rest, taken from the remainder before the watcher has looked once.
    rest = SW.sale.terms.total_atoms - first_buy
    moved = copy.deepcopy(SW.sale)
    moved.funding = {"txid": tx1, "vout": 1, "atoms": rest}
    moved.locked_atoms = rest
    (allowed, why), hexed2 = raw_buy(moved, rest)
    ok.ok(allowed, "and so is a full buy of the remainder", why)
    w("sendrawtransaction", hexed2)
    rig.mine()
    watch.poll()
    rig.mine()
    watch.poll()
    ok.eq(SW.sale.status, S.SOLD_OUT, "two moves between two polls is a sell-out, not a ghost")
    ok.eq(SW.sale.sold_atoms, SW.sale.terms.total_atoms, "with everything sold")
    mine_ = [q for q in plat.positions("buyer", tier)["positions"] if q["slug"] == "swift"]
    ok.eq(len(mine_), 1, "and the buyer keeps their allocation in it")
    ok.eq(mine_[0]["tokens_atoms"], first_buy, "for the tokens they bought")

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
    except TX.BuildError as e:
        ok.ok("block height" in str(e),
              "a time locktime on a height-closed sale is refused for being the wrong kind",
              str(e))


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
