#!/usr/bin/env python3
"""A purchase made the way a person makes one: in a browser, with a wallet.

Everything else here proves a piece. The unit tests prove the arithmetic, the
API drill proves the endpoints, the node suite proves the chain accepts what
Levo builds, and the render test proves the pages paint. None of them presses
the buttons, and the browser path is the one where a person authorises a
payment: sign in, price it, let the wallet pick outputs, build, sign the PSET,
broadcast, and have Levo record it. Every one of those steps crosses between
the app, the wallet and levod, and a rename on either side of any of those
crossings is invisible to every other test in this repository.

So this starts a real sequentiad, a real levod serving the built app, and a
headless Chromium driven over the debugging protocol, and installs a wallet
that is fake only in where it lives: it signs with the node's own keys, spends
the node's own outputs, and broadcasts to the node. The purchase that comes out
is a purchase, on a chain, and it is checked as one.

Skipped, not failed, when there is no node binary, no Chromium, or no built app
-- the same way the node and render suites skip.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cdp  # noqa: E402
from test_cli import Checker  # noqa: E402
from test_node import COIN, Rig, find_node, free_port  # noqa: E402
from test_render import find_chromium  # noqa: E402

# The wallet the page believes it is talking to. Nothing here signs or decides
# anything: every call is queued for the test process, which answers it with
# the node. Written as one string because it has to be installed before the
# app's own scripts run, which is the only thing an extension really does
# differently from a script on the page.
WALLET_JS = """
window.__wallet = { queue: [], next: 1, pending: {} };
window.__walletTake = function () {
  const q = window.__wallet.queue; window.__wallet.queue = []; return JSON.stringify(q);
};
window.__walletAnswer = function (id, ok, value) {
  const p = window.__wallet.pending[id];
  if (!p) return false;
  delete window.__wallet.pending[id];
  if (ok) { p.resolve(value); } else { p.reject(new Error(String(value))); }
  return true;
};
window.sequentia = {
  isSequentia: true,
  request: function (req) {
    const method = req && req.method;
    const params = (req && req.params) || {};
    return new Promise(function (resolve, reject) {
      const id = window.__wallet.next++;
      window.__wallet.pending[id] = { resolve: resolve, reject: reject };
      window.__wallet.queue.push({ id: id, method: method, params: params });
    });
  },
};
"""


class Levod:
    """The real server against the rig's node, serving the built app."""

    def __init__(self, rig, port, payment_asset, operator=None):
        self.state = Path(tempfile.mkdtemp()) / "state.json"
        env = dict(
            os.environ,
            LEVOD_PORT=str(port), LEVOD_HOST="127.0.0.1",
            LEVOD_STATE=str(self.state), LEVOD_SECRET="browser-test-secret",
            LEVOD_RPC_URL=rig.url, LEVOD_RPC_USER="levo", LEVOD_RPC_PASSWORD="levo",
            LEVOD_PAYMENT_ASSET=payment_asset, LEVOD_PAYMENT_LABEL="PAY",
            LEVOD_HRP="ert", LEVOD_WATCH_SECONDS="2",
            LEVOD_WEBROOT=str(ROOT / "web" / "dist"),
            LEVOD_CHAIN_TTL="0",
            LEVOD_TIERS=json.dumps([{"name": "Everyone", "min_stake": 0,
                                     "cap": 1000000, "may_list": True}]),
            # The browser's own account is an operator here, so the test can
            # open the ledger panel -- the issuer-only view that shipped a
            # blank page because nothing could reach it.
            LEVOD_OPERATORS=operator or "",
        )
        self.log = open(os.path.join(tempfile.mkdtemp(), "levod.log"), "w+")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "levod" / "server.py")],
                                     stdout=self.log, stderr=subprocess.STDOUT, env=env)
        self.url = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                urllib.request.urlopen(self.url + "/api/health", timeout=2).read()
                return
            except Exception:
                if self.proc.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError("levod exited:\n" + self.log.read())
                time.sleep(0.25)
        raise RuntimeError("levod did not start")

    def api(self, path):
        return json.loads(urllib.request.urlopen(self.url + path, timeout=10).read())

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
        self.log.close()


class Wallet:
    """What the page's wallet calls actually do: the node, in this process."""

    def __init__(self, rig, payment_asset, key=None, account=None):
        self.rig = rig
        self.payment_asset = payment_asset
        self.seen = []
        self.broadcast = []               # every txid this wallet actually relayed
        self.key = key
        self.account = account

    def handle(self, method, params):
        self.seen.append(method)
        w, n = self.rig.w, self.rig.n
        if method == "connect":
            return {"connected": True}
        if method == "getCapabilities":
            return {"methods": ["connect", "getAddress", "getUtxos", "signPset",
                                "broadcast", "signMessage", "getCapabilities"],
                    "features": ["pset-site-built"]}
        if method == "getAddress":
            return {"address": w("getnewaddress", "", "bech32")}
        if method == "getUtxos":
            out = []
            for u in w("listunspent", 1):
                if u.get("amountcommitment") or u.get("assetcommitment"):
                    continue          # a confidential output states nothing
                out.append({"txid": u["txid"], "vout": int(u["vout"]),
                            "asset": (u.get("asset") or "").lower(),
                            "value": str(int(round(float(u["amount"]) * 1e8)))})
            return {"utxos": out}
        if method == "signMessage":
            return {"signature": n("signmessagewithprivkey", self.key,
                                   params["message"])}
        if method == "signPset":
            r = w("walletprocesspsbt", params["pset"])
            return {"pset": r["psbt"]}
        if method == "broadcast":
            if params.get("pset"):
                fin = w("finalizepsbt", params["pset"])
                if not fin.get("complete"):
                    raise RuntimeError("the wallet could not finalise the PSET")
                txid = n("sendrawtransaction", fin["hex"])
            else:
                txid = n("sendrawtransaction", params["hex"])
            self.broadcast.append(txid)
            return {"txid": txid}
        raise RuntimeError("this wallet does not support " + method)


def pump(page, wallet, times=1):
    """Answer whatever the page has asked its wallet for."""
    for _ in range(times):
        queued = json.loads(page.eval("window.__walletTake ? window.__walletTake() : '[]'") or "[]")
        for req in queued:
            try:
                value = wallet.handle(req["method"], req.get("params") or {})
                ok, payload = True, value
            except Exception as e:
                ok, payload = False, str(e)
            page.eval("window.__walletAnswer(%d, %s, %s)"
                      % (req["id"], "true" if ok else "false", json.dumps(payload)))
        if not queued:
            return
        time.sleep(0.1)


def wait(page, wallet, expr, timeout=60, every=0.25):
    """Wait for the page, answering the wallet while waiting -- because what
    the page is waiting for is usually the wallet."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        pump(page, wallet, times=3)
        try:
            last = page.eval(expr)
            if last:
                return last
        except cdp.BrowserError as e:
            last = str(e)
        time.sleep(every)
    raise cdp.BrowserError("waited %ss for %s (last %r); page said: %s"
                           % (timeout, expr, last, page.text()[-400:]))


def has(page, text):
    return "document.body.innerText.toLowerCase().includes(%s)" % json.dumps(text.lower())


def run(ok, rig, levod, page, wallet, slug):
    page.go(levod.url + "/p/" + slug, settle=2.0)
    ok.ok("Helios" in page.text() or slug in page.text().lower(),
          "the sale page paints", page.text()[:120])

    # --- signing in is a signature over levod's own challenge --------------
    #
    # The buy panel is the sign-in panel until there is a session, which is
    # itself the thing being checked: a visitor lands on a sale and the first
    # thing they are asked for is a signature, not a password.
    page.click("Sign in with your wallet")
    wait(page, wallet, "!!document.querySelector('#qty')", timeout=90)
    ok.ok("signMessage" in wallet.seen, "the app asked the wallet to sign the challenge")

    # --- price it ----------------------------------------------------------
    page.fill("#qty", "100")
    page.click("Price this purchase")
    wait(page, wallet, has(page, "you pay"), timeout=60)
    ok.ok("you pay" in page.text().lower(), "the plan says what it costs")

    # --- the wallet picks the outputs, and the node vets them --------------
    page.click("Fill from my wallet")
    wait(page, wallet, "!!document.querySelector('#inp') && "
                       "document.querySelector('#inp').value.length > 10", timeout=60)
    ok.ok("getUtxos" in wallet.seen and "getAddress" in wallet.seen,
          "the wallet supplied the outputs and an address")

    # --- build, sign, broadcast -------------------------------------------
    page.click("Build it")
    wait(page, wallet, has(page, "sign and broadcast"), timeout=90)
    ok.ok(True, "levod built the transaction and the page offered it for signing")

    page.click("Sign and broadcast with my wallet")
    wait(page, wallet, has(page, "broadcast."), timeout=120)
    ok.ok("signPset" in wallet.seen, "the wallet signed the PSET the site built")
    ok.ok("broadcast" in wallet.seen, "and broadcast it")

    # The transaction the wallet actually relayed, which is the only txid this
    # test will believe -- and the page has to be showing that one.
    ok.eq(len(wallet.broadcast), 1, "the wallet broadcast exactly one transaction")
    txid = wallet.broadcast[0]
    ok.ok(page.eval("document.body.innerText.includes(%s)" % json.dumps(txid[:16])),
          "the page shows the transaction it broadcast", txid)
    ok.ok(txid in rig.n("getrawmempool"), "the node has it in its mempool")

    # The covenant's own rule, in the transaction a browser just made: output 0
    # pays the treasury the published price, and output 1 re-rests the
    # remainder at the same address the sale was resting at.
    raw = rig.n("decoderawtransaction", rig.w("gettransaction", txid, True, True)["hex"])
    terms = levod.api("/api/projects/" + slug)["sale"]["terms"]
    treasury = raw["vout"][0]
    ok.eq((treasury.get("asset") or "").lower(), terms["payment_asset"],
          "output 0 is in the payment asset")
    ok.eq(int(round(float(treasury["value"]) * 1e8)), 25 * COIN,
          "and pays what the covenant's price demands for 100 tokens")
    rest = raw["vout"][1]
    ok.eq((rest.get("scriptPubKey") or {}).get("hex"),
          levod.api("/api/projects/" + slug)["sale"]["script_pubkey"],
          "output 1 re-rests the remainder at the sale's own address")
    rig.mine()
    time.sleep(3)                            # the watcher polls every 2s here
    sale = levod.api("/api/projects/" + slug)["sale"]
    ok.eq(sale["status"], "partial", "the sale reads partial after it")
    ok.eq(int(sale["locked_atoms"]), 900 * COIN, "and rests on the remainder")
    ok.ok(int(sale["sold_atoms"]) >= 100 * COIN, "with the sold figure moved",
          sale["sold_atoms"])

    # --- and Levo recorded it against the account that made it -------------
    #
    # The ledger is what a per-buyer cap is measured against, and recording is
    # the one step that happens after the money has moved. A page that says
    # "broadcast" and leaves the ledger empty is the failure this checks for.
    ok.ok("record it against my cap" not in page.text().lower(),
          "the purchase recorded itself without asking the buyer to")
    ledger = levod.api("/api/projects/" + slug)["sale"]
    t = time.time() + 30
    while time.time() < t and int(ledger.get("sold_atoms") or 0) < 100 * COIN:
        time.sleep(1)
        ledger = levod.api("/api/projects/" + slug)["sale"]

    # The account page is where a buyer looks for what they hold. It is a
    # different endpoint reading the same ledger, so a purchase that recorded
    # itself but does not appear there is still a purchase the buyer cannot see.
    page.go(levod.url + "/account", settle=2.0)
    wait(page, wallet, has(page, "helios"), timeout=60)
    ok.ok("100" in page.text(), "the account page shows the position it just bought",
          page.text()[:200])

    # --- the issuer's own view of the ledger -------------------------------
    #
    # This panel is behind a session that only an issuer or an operator has,
    # which is why a page-painting test could never reach it -- and it shipped
    # calling a helper it never imported, so opening it unmounted the entire
    # sale page, nav and footer included.
    page.go(levod.url + "/p/" + slug, settle=2.0)
    page.click("See what Levo recorded")
    wait(page, wallet, has(page, "what levo recorded"), timeout=60)
    ok.ok(len(page.text()) > 400, "the sale page survives opening its own ledger",
          page.text()[:200])
    ok.ok("for" in page.text().lower() and "HLX" in page.text(),
          "and the ledger lists the purchase")

    errors = page.errors()
    ok.eq(errors, [], "the whole purchase ran with no console error")


def setup_sale(rig, levod, ok):
    """A funded sale to buy from, made through the API the way the CLI does."""
    w, n = rig.w, rig.n
    token = w("issueasset", assetamount=100_000, tokenamount=0, blind=False,
              fee_asset="bitcoin")["asset"]
    rig.mine()
    env = dict(os.environ, LEVO_URL=levod.url,
               SEQUENTIA_CLI=str(Path(rig.binary).parent / "sequentia-cli"),
               SEQUENTIA_DATADIR=rig.root, SEQUENTIA_WALLET="levo",
               LEVO_SESSION=os.path.join(tempfile.mkdtemp(), "session.json"))
    keys = json.loads(subprocess.run(
        [sys.executable, str(ROOT / "bin" / "levo"), "keygen"],
        capture_output=True, text=True, env=env, timeout=60).stdout)
    spec = {
        "project": {"slug": "helios-grid", "name": "Helios Grid", "ticker": "HLX",
                    "decimals": 8, "summary": "Solar microgrids, in a browser.",
                    "description": "The sale this browser test buys from."},
        "terms": {"token_asset": token, "price_num": 1, "price_den": 4,
                  "min_lot": 10 * COIN, "total_atoms": 1_000 * COIN,
                  "close_locktime": n("getblockcount") + 500,
                  "treasury_address": w("getnewaddress", "", "bech32"),
                  "reclaim_xonly": keys["reclaim_xonly"]},
    }
    listing = Path(tempfile.mkdtemp()) / "listing.json"
    listing.write_text(json.dumps(spec))
    for args in (["create", str(listing)], ["lock", "helios-grid"]):
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "levo")] + args,
                           capture_output=True, text=True, env=env, timeout=180)
        if r.returncode != 0:
            raise RuntimeError("levo %s failed: %s" % (args[0], (r.stdout + r.stderr)[-400:]))
    rig.mine()
    return "helios-grid"


def main():
    binary = find_node()
    chromium = find_chromium()
    if not binary:
        print("no sequentiad found; skipping the browser test "
              "(set SEQUENTIAD or SEQUENTIA_SRC)")
        return 0
    if not chromium:
        print("no Chromium found; skipping the browser test (set LEVO_CHROMIUM)")
        return 0
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        print("no built app in web/dist; skipping the browser test "
              "(npm --prefix web run build)")
        return 0
    ok = Checker()
    rig = Rig(binary)
    levod = page = None
    try:
        pay = rig.w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False,
                    fee_asset="bitcoin")["asset"]
        rig.mine()
        rig.n("setfeeexchangerates", {"bitcoin": COIN, pay: COIN})
        Path(rig.root, "elements.conf").write_text(
            "chain=elementsregtest\n[elementsregtest]\n"
            "rpcport=%d\nrpcuser=levo\nrpcpassword=levo\n" % rig.port)
        # The browser's identity, made before levod starts so levod can be
        # told about it. A key written into a test is a key for one chain's
        # address version, and this suite runs on a custom chain.
        addr = rig.w("getnewaddress", "", "bech32")
        account = rig.w("getaddressinfo", addr)["pubkey"]
        levod = Levod(rig, free_port(), pay, operator=account)
        slug = setup_sale(rig, levod, ok)
        page = cdp.Page(chromium)
        page.on_new_document(WALLET_JS)
        run(ok, rig, levod, page,
            Wallet(rig, pay, key=rig.w("dumpprivkey", addr), account=account), slug)
    except Exception:
        import traceback
        ok.failed.append("the drive raised:\n" + traceback.format_exc())
    finally:
        if page:
            page.stop()
        if levod:
            levod.stop()
        rig.stop()
    return ok.report()


if __name__ == "__main__":
    sys.exit(main())
