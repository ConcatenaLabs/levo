#!/usr/bin/env python3
"""A vesting Lightning sale end to end, against a real Arca operator.

The Sequentia side is an Arca operator kept running for outside drivers (arca's
`bark-cli/tests/arca_operator_for_browsers.rs`, started with
`ARCA_OPERATOR_NO_SWEEP` and `ARCA_OPERATOR_MORE_ASSETS=1`: batches without a
sweep, the only ones that hold vesting claims, and a token the operator
serves). The Bitcoin side is test_lightning_e2e's: bitcoind, the project's
lightningd with the holdinvoice plugin, and a buyer's.

A buyer who holds only BTC orders a lot of a sale that vests and pays the
invoice. The receiver moves the lot out of the sale into the project's Arca
wallet, boards it, pays it to the buyer's key as a vesting claim, and settles
only once the operator has released the claim. The buyer's Arca wallet finds
the claim before the vesting date, with the date, and cannot take it up; after
the date it takes it up and spends it. A direct purchase of the sale is
refused, and its leaf purchase too.

    ARCA_OPERATOR_CONTROL=127.0.0.1:18745 ARCA_CLI=.../target/debug/arca \\
        python3 levod/tests/test_vest_e2e.py

Skipped without the operator, `arca`, bitcoind, lightningd or the plugin.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import market as M  # noqa: E402
import receiver as RCV  # noqa: E402
import rpc as RPCMOD  # noqa: E402
import secp256k1 as EC  # noqa: E402
import store as ST  # noqa: E402
from test_lightning_e2e import Lightning, find_holdplugin, find_lightningd, free_port  # noqa: E402
from test_node import Checker, Founder  # noqa: E402

COIN = 100_000_000
RECEIVER_SECRET = "67" * 32


class Operator:
    def __init__(self, control):
        self.base = "http://" + control

    def __call__(self, path, body=None, method="POST"):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if method == "GET" else json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read() or b"null")


class Wallet:
    """An Arca wallet through the `arca` command line, in its own directory."""

    def __init__(self, arca, root, name, state):
        self.dir = os.path.join(root, name)
        self.arca = arca
        self.name = name
        env = dict(os.environ, ARCA_NODE_PASSWORD=state["node_password"])
        self.env = env
        self.ok("create", "--server", state["server"], "--node-url", state["node_url"],
                "--node-user", state["node_user"])

    @property
    def command(self):
        return "%s --datadir %s" % (self.arca, self.dir)

    def ok(self, *args, stdin=None):
        r = subprocess.run([self.arca, "--datadir", self.dir] + list(args), input=stdin,
                           capture_output=True, text=True, env=self.env, timeout=900)
        if r.returncode != 0:
            raise RuntimeError("%s: arca %s: %s" % (self.name, " ".join(args), r.stderr.strip()[-500:]))
        return json.loads(r.stdout)


def run(ok, op, arca, ln, log, root):
    state = op("/state", method="GET")
    node = RPCMOD.NodeRPC(url=state["node_url"], user=state["node_user"], password=state["node_password"])
    node.chain_info_ttl = 0
    x, token = state["x"], state["more"][0]

    def produce(n=1):
        for _ in range(n):
            op("/produce")

    def median():
        return int(node.call("getblockchaininfo")["mediantime"])

    plat = M.Platform(ST.Store(os.path.join(root, "levo-state.json")), Founder(), None, node,
                      hrp="ert", payment_asset=x, payment_label="X")
    project_arca = Wallet(arca, root, "project", state)
    rport = free_port()

    def sale_of(slug):
        sale = plat.projects[slug].sale
        f = sale.funding
        return sale.terms, {"txid": f["txid"], "vout": f["vout"], "atoms": sale.locked_atoms}
    rcv = RCV.Receiver(RECEIVER_SECRET, RCV.Cln(ln.receiver_command()), node, sale_of,
                       os.path.join(root, "receiver.json"), ["vest"], invoice_expiry=600,
                       arca=RCV.Arca(project_arca.command), vest_days=30)
    srv = ThreadingHTTPServer(("127.0.0.1", rport), RCV.make_handler(rcv, "ert"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(3):
        op("/fund", {"script": rcv.own_spk.hex(), "asset": x, "amount": 1_000_000})
    # The operator funds each round's batch from its own coins of the asset.
    op("/fund_server", {"asset": token, "amount": 2_000 * COIN})
    produce()

    vest = median() + 6 * 3600
    h0 = node.chain_height()
    terms = {"token_asset": token, "payment_asset": x, "price_num": 10, "price_den": 1,
             "treasury_prog": EC.xonly_pubkey(0x55).hex(), "min_lot": 10 * COIN,
             "close_locktime": h0 + 5000, "reclaim_xonly": EC.xonly_pubkey(0x77).hex(),
             "total_atoms": 1_000 * COIN,
             "lightning": {"receiver_xonly": rcv.xonly, "receiver_url": "http://127.0.0.1:%d" % rport,
                           "btc_num": 1, "btc_den": 100000, "bond_asset": x,
                           "bond_atoms": 30_000, "fee_allowance": 20_000,
                           "timeout_seconds": 3600, "vest_locktime": vest}}
    issuer = "02" + "11" * 32
    plat.list_project(issuer, {"slug": "vest", "name": "Vest", "ticker": "VST",
                               "summary": "s", "description": "d"}, terms)
    P = plat.projects["vest"]
    ok.eq(P.sale.cov.sell_leaf, None, "a sale that vests has no SELL leaf")
    op("/fund", {"script": P.sale.script_pubkey, "asset": token, "amount": 1_000 * COIN})
    produce()
    plat.confirm_lock(issuer, "vest")
    ok.eq(P.sale.status, "live", "the vesting sale is listed and live")
    log.append("sale vest at %s, vests at median time %d" % (plat.sale_address(P.sale), vest))

    holder = Wallet(arca, root, "holder", state)
    buyer_key = "02" + holder.ok("address")["key"]

    # --- the ways around the vesting are closed -------------------------------
    for what, fn in (("a direct purchase", lambda: plat.plan_buy(buyer_key, "vest", token_atoms=str(10 * COIN))),):
        try:
            fn()
            ok.ok(False, "%s of a vesting sale is refused" % what)
        except M.PlatformError as e:
            ok.ok("vests" in str(e), "%s of a vesting sale is refused" % what, str(e))
            log.append("refused: %s -> %s" % (what, e))
    ok.eq(plat.leaf_order("vest").get("fillable"), False, "and so is its leaf purchase")

    # --- the buyer orders and pays in BTC --------------------------------------
    lot = plat.lightning_reserve(buyer_key, "vest", {"token_atoms": str(100 * COIN)})["lot"]
    ok.eq(lot["buyer_key"], buyer_key, "a vesting lot is ordered for the buyer's key, no address asked")
    h = lot["payment_hash"]
    res = {}

    def go():
        try:
            # Held while the lot is moved in, boarded and paid in a round:
            # minutes, past pay's default minute of patience.
            res["r"] = ln.cli(1, "-k", "pay", "bolt11=" + lot["bolt11"], "retry_for=1800")
        except Exception as e:
            res["e"] = str(e)
    th = threading.Thread(target=go, daemon=True)
    th.start()

    def until(cond, what, secs=120):
        end = time.time() + secs
        while time.time() < end:
            v = cond()
            if v:
                return v
            time.sleep(0.5)
        raise RuntimeError("timed out waiting for " + what)
    until(lambda: plat.lightning_status("vest", h)["state"] == "paid", "the payment to be held")
    done = rcv.poll()
    ok.ok(done and done[0][0] == "stocking", "the receiver moves the lot into the project's Arca wallet", str(done))
    stock_txid = rcv.lots[h]["stock_txid"]
    produce()
    done = rcv.poll()
    ok.ok(done and done[0][0] == "boarding", "and boards it once the move is in a block", str(done))
    log.append("moved %d atoms out of the sale (%s) and boarded them"
               % (rcv.lots[h]["stock_atoms"], stock_txid))

    # Drive the operator until the claim is released: the board is credited,
    # the distribution's round built, its forfeits handed over by the
    # receiver's sync, the round mined and buried.
    states = []
    for i in range(30):
        produce()
        op("/bury")
        b = op("/build")
        if b.get("rounds") or b.get("failed"):
            log.append("operator build: %s" % json.dumps(b)[:400])
        done = rcv.poll()
        states += [d[0] for d in done]
        op("/pass")
        produce()
        op("/bury")
        done = rcv.poll()
        states += [d[0] for d in done]
        if os.environ.get("LEVO_VEST_TRACE"):
            ds = project_arca.ok("distributions")
            print("trace %d: lot %s, distributions %s, participations %s" % (
                i, rcv.lots[h]["state"], [(d.get("state"), d.get("wallet_state"), d.get("round"))
                                          for d in ds],
                json.dumps(project_arca.ok("participations"))[:600]), flush=True)
        if rcv.lots[h]["state"] == "delivered":
            break
        time.sleep(1)
    ok.ok("distributing" in states, "the lot is paid as a vesting claim to the buyer's key", str(states))
    ok.ok(rcv.lots[h]["state"] == "delivered", "the payment settles once the claim is released",
          json.dumps({k: v for k, v in rcv.lots[h].items() if k != "preimage"}))
    th.join(120)
    pays = ln.cli(1, "listpays", lot["bolt11"])["pays"]
    ok.eq([p["status"] for p in pays], ["complete"], "the buyer's Lightning payment completes")
    log.append("the buyer's payment: %s, %s msat" % (pays[0]["status"], pays[0].get("amount_sent_msat")))
    st = plat.lightning_status("vest", h)
    ok.eq(st["state"], "delivered", "the order reads delivered")
    dist = [d for d in project_arca.ok("distributions") if d["distribution_id"] == st["distribution_id"]][0]
    log.append("distribution %s: claims %s, vest %s, deadline %s, round %s"
               % (dist["distribution_id"], [c["state"] for c in dist["claims"]], dist.get("vest"),
                  dist.get("lapse_deadline"), dist.get("round")))
    ok.eq(dist.get("vest"), vest, "the claim vests at the sale's date")

    # --- the buyer's wallet before the date -------------------------------------
    found = holder.ok("claims")
    mine = [c for c in found if c.get("asset") == token]
    ok.ok(len(mine) == 1, "the buyer's wallet finds the lot", json.dumps(found))
    c = mine[0]
    ok.eq(c["state"], "vesting", "as vesting")
    ok.eq(int(c["value"]), 100 * COIN, "the whole lot")
    ok.ok(bool(c.get("vests")), "with its date in words: %s" % c.get("vests"))
    log.append("holder's claims before the date: %s %s, %s, vests on %s"
               % (c["value"], token[:8], c["state"], c["vests"]))
    taken = holder.ok("claim")
    ok.ok(all(t.get("state") == "vesting" for t in taken if t.get("claim")),
          "taking it up before the date takes nothing", json.dumps(taken))
    log.append("holder's claim before the date: %s" % json.dumps(taken)[:300])

    # --- after the date --------------------------------------------------------
    op("/advance", {"seconds": vest - median() + 1200})
    produce(3)
    op("/bury")
    op("/settle")
    taken = holder.ok("claim")
    ok.ok(any(t.get("taken_up") for t in taken), "after the date the wallet takes it up", json.dumps(taken))
    log.append("holder's claim after the date: %s" % json.dumps(taken)[:400])
    # The claim's coin is paid on out of round to a fresh coin of the
    # holder's; a sync makes it live.
    for _ in range(5):
        holder.ok("sync")
        coins = [c for c in holder.ok("coins") if c.get("asset") == token and c.get("state") == "live"]
        if coins:
            break
        produce()
        op("/bury")
    log.append("holder's live coins of the token after the date: %s"
               % [(c.get("value"), c.get("state")) for c in coins])
    other = Wallet(arca, root, "other", state)
    req = other.ok("receive", "--asset", token, "--amount", str(40 * COIN))
    request = req["request"]
    sent = holder.ok("send", request, "--amount", str(40 * COIN), "--asset", token)
    ok.ok(bool(sent), "and spends it: 40 VST paid to another wallet", json.dumps(sent)[:300])
    log.append("after the date: taken up, then 40 VST sent: %s" % json.dumps(sent)[:300])


def main():
    control, arca = os.environ.get("ARCA_OPERATOR_CONTROL"), os.environ.get("ARCA_CLI")
    lnd, hold = find_lightningd(), find_holdplugin()
    if not control or not arca or not lnd or not hold or not shutil.which("bitcoind"):
        print("skipped: needs ARCA_OPERATOR_CONTROL (a running arca_operator_for_browsers with "
              "ARCA_OPERATOR_NO_SWEEP and ARCA_OPERATOR_MORE_ASSETS), ARCA_CLI, bitcoind, "
              "lightningd and the holdinvoice plugin")
        return 0
    ok, log = Checker(), []
    root = tempfile.mkdtemp(prefix="levo-vest-")
    ln = None
    try:
        ln = Lightning(lnd, hold)
        run(ok, Operator(control), arca, ln, log, root)
    finally:
        if ln:
            ln.stop()
        out = os.environ.get("LEVO_VEST_LOG")
        if out:
            Path(out).write_text("\n".join(log) + "\n")
        for line in log:
            print("  " + line)
        if not os.environ.get("LEVO_VEST_KEEP"):
            shutil.rmtree(root, ignore_errors=True)
    return ok.report()


if __name__ == "__main__":
    sys.exit(main())
