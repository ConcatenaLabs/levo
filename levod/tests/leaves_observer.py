#!/usr/bin/env python3
"""Buying from leaves, end to end: the observer of arca's shared-fills test.

arca's `bark-cli/tests/arca_shared_fills.rs` runs an anchored regtest chain, an
operator and eleven wallets, and runs the program named by ARCA_TEST_OBSERVER
at each step: the step's name as its argument, its facts as JSON on standard
input, an answer as JSON on standard output. This is that program for Levo. It
starts a levod serving the built app on the test's chain and reading the
test's operator (LEVOD_LEAF_OPERATOR), lists the sale the wallets take, records
each take against its buyer's account, drives one buyer through the sale page
in developer mode in a headless Chromium (its stand-in wallet answers
`takeOrder` by running that wallet's own `arca take`, after checking the page
shows no purchase from leaves outside developer mode), and at every step
checks what levod shows against what the step says the chain holds. A step
whose expectation levod does not meet within a minute and a half exits 1,
which fails the test.

    ARCA_TEST_OBSERVER=levod/tests/leaves_observer.py \
      cargo test -p arca-cli --test arca_shared_fills -- --nocapture

OBSERVER_DIR is where levod's state, its log and this program's own state go
(by default a directory under the system's temporary one, named for the test
process); LEVOD_DIR is this checkout (by default, the one this file is in).
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

LEVOD_DIR = os.environ.get("LEVOD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.environ.get("OBSERVER_DIR") or os.path.join(tempfile.gettempdir(), "levo-leaves-observer-%d" % os.getppid())
STATE = os.path.join(WORK, "observer-state.json")
sys.path.insert(0, os.path.join(LEVOD_DIR, "levod", "tests"))
sys.path.insert(0, os.path.join(LEVOD_DIR, "levod"))
import signhelper  # noqa: E402


def _port():
    try:
        return int(json.load(open(STATE))["port"])
    except Exception:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


PORT = _port()
URL = "http://127.0.0.1:%d" % PORT


def log(*a):
    print("[levod observer]", *a, file=sys.stderr, flush=True)


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save(st):
    json.dump(st, open(STATE, "w"), indent=1)


def http(method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def secret(label):
    return int.from_bytes(hashlib.sha256(("levo e2e/" + label).encode()).digest(), "big") % signhelper.N


def session(st, label):
    tok = st.setdefault("tokens", {}).get(label)
    if tok:
        return tok
    s, ch = http("POST", "/api/auth/challenge", {})
    assert s == 200, ch
    sec = secret(label)
    s, v = http("POST", "/api/auth/verify", {"message": ch["message"], "signature": signhelper.sign_recoverable(sec, ch["message"])})
    assert s == 200, v
    st["tokens"][label] = v["token"]
    st.setdefault("accounts", {})[label] = v["account"]
    save(st)
    return v["token"]


def wait(what, check, seconds=90):
    start = time.time()
    last = None
    while time.time() - start < seconds:
        ok, last = check()
        if ok:
            return last
        time.sleep(0.5)
    log("TIMED OUT waiting for", what, "last:", json.dumps(last)[:2000])
    sys.exit(1)


def step_start(f):
    global PORT, URL
    os.makedirs(WORK, exist_ok=True)
    if os.path.exists(STATE):
        os.remove(STATE)
    PORT = _port()
    URL = "http://127.0.0.1:%d" % PORT
    st = {"facts": f, "port": PORT}
    env = dict(os.environ, LEVOD_HOST="127.0.0.1", LEVOD_PORT=str(PORT), LEVOD_STATE=os.path.join(WORK, "levo-state.json"),
               LEVOD_SECRET="00" * 32, LEVOD_RPC_URL=f["node_url"].rstrip("/"), LEVOD_RPC_USER=f["node_user"],
               LEVOD_RPC_PASSWORD=f["node_password"], LEVOD_PAYMENT_ASSET=f["payment"], LEVOD_PAYMENT_LABEL="X",
               LEVOD_PAYMENT_DECIMALS="8", LEVOD_HRP="ert", LEVOD_WATCH_SECONDS="1", LEVOD_CHAIN_TTL="0",
               LEVOD_WEBROOT=os.path.join(LEVOD_DIR, "web", "dist"),
               LEVOD_LEAF_OPERATOR=f["operator_url"].rstrip("/"), LEVOD_ORIGIN=URL,
               LEVOD_TIERS=json.dumps([{"name": "Open", "min_stake": 0, "cap": 1000, "may_list": True, "blurb": "every account"}]))
    for k in ("LEVOD_RPC_COOKIE",):
        env.pop(k, None)
    out = open(os.path.join(WORK, "levod.log"), "ab")
    p = subprocess.Popen([sys.executable, os.path.join(LEVOD_DIR, "levod", "server.py")], env=env, stdout=out, stderr=out,
                         start_new_session=True)
    st["pid"] = p.pid
    save(st)
    open(os.path.join(WORK, "levod.pid"), "w").write(str(p.pid))
    def healthy():
        if not _up():
            return (False, None)
        r = http("GET", "/api/health")
        return (r[0] == 200, r)
    wait("levod's health", healthy, 60)
    s, cfg = http("GET", "/api/config")
    log("levod %s (pid %d) up: leaves %s" % (URL, p.pid, cfg.get("leaves")))
    assert cfg.get("leaves") is True
    return {"levod": URL, "pid": p.pid, "leaves": cfg.get("leaves")}


def _up():
    try:
        urllib.request.urlopen(URL + "/api/health", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def step_order(f):
    st = load()
    if not st.get("slug"):
        tok = session(st, "issuer")
        terms = {"token_asset": f["asset_a"], "price_num": f["rate_num"], "price_den": f["rate_den"], "min_lot": f["min_lot"],
                 "total_atoms": f["locked"], "close_locktime": f["expiry_locktime"], "reclaim_xonly": f["maker_x"],
                 "treasury_prog": f["maker_prog"], "treasury_ver": 1}
        project = {"slug": "leaf-sale", "name": "Leaf Sale", "ticker": "YYY", "decimals": 8,
                   "summary": "A sale for many buyers", "description": "Ten buyers in one round.", "links": {}}
        s, d = http("POST", "/api/projects", {"project": project, "terms": terms}, tok)
        assert s == 201, d
        spk = d["project"]["sale"]["script_pubkey"]
        log("listed %s: Levo derives the sale address %s; the test's order rests at %s" % (project["slug"], spk, f["script_pubkey"]))
        assert spk == f["script_pubkey"], "Levo's sale address is not the order's"
        txid, vout = f["outpoint"].split(":")
        s, d = http("POST", "/api/projects/leaf-sale/lock", {"txid": txid, "vout": int(vout)}, tok)
        assert s == 200, d
        st["slug"] = "leaf-sale"
        save(st)
        log("locked: %s %s holding %s" % (d["sale"]["status"], d["sale"]["funding"]["txid"][:16], d["sale"]["locked_atoms"]))
    want = f["outpoint"]
    def at():
        s, d = http("GET", "/api/projects/leaf-sale/order")
        o = (d or {}).get("order") or {}
        c = o.get("covenant") or {}
        return ("%s:%s" % (c.get("covenant_txid"), c.get("covenant_vout")) == want, d)
    d = wait("levod's order at %s" % want, at)
    log("the sale as an order, from levod: %s:%s, min_lot %s, %s resting" % (d["order"]["covenant"]["covenant_txid"][:16],
        d["order"]["covenant"]["covenant_vout"], d["min_lot"], d["locked_atoms"]))
    return {"order": d["order"]}


def buyer(wallet):
    return "buyer " + wallet


def step_take(f):
    st = load()
    tok = session(st, buyer(f["wallet"]))
    s, d = http("POST", "/api/projects/leaf-sale/takes", {"take": f["take"]}, tok)
    assert s == 200, d
    st.setdefault("takes", {})[f["take"]] = {"wallet": f["wallet"], "account": st["accounts"][buyer(f["wallet"])],
                                             "filled": f["filled"]}
    save(st)
    log("%s's take %s recorded against account %s: %s, holding %s of its headroom (allowance left %s)" % (
        f["wallet"], f["take"][:16], st["accounts"][buyer(f["wallet"])][:16], d["take"]["state"], d["take"]["payment_atoms"],
        d["allowance_remaining_atoms"]))
    return {"state": d["take"]["state"]}


def ledger(st):
    s, d = http("GET", "/api/projects/leaf-sale/purchases?limit=200", token=session(st, "issuer"))
    assert s == 200, d
    return d


def step_round(f):
    st = load()
    want = {"built": ("pending",), "mempool": ("in_round",), "mined": ("confirmed",), "out": ("in_round", "undone"),
            "final": ("confirmed",)}[f["state"]]
    takes = f["takes"]
    def check():
        d = ledger(st)
        by = {t["take"]: t for t in d["takes"]}
        states = {t: (by.get(t) or {}).get("state") for t in takes}
        return (all(s in want for s in states.values()), {"states": states, "ledger": d})
    got = wait("takes %s" % (want,), check)
    d = got["ledger"]
    s, p = http("GET", "/api/projects/leaf-sale")
    sale = p["sale"]
    counted = [e for e in d["purchases"] if e.get("take") in takes and not e.get("undone")]
    log("round %s %s: %d takes %s; the sale %s, %s sold, %s resting at %s:%s, buyers %s, takes by state %s" % (
        f["txid"][:16], f["state"], len(takes), "/".join(sorted(set(got["states"].values()))), sale["status"], sale["sold_atoms"],
        sale["locked_atoms"], sale["funding"]["txid"][:16], sale["funding"]["vout"], sale["buyers"], sale["takes"]))
    if f["state"] in ("mined", "final"):
        assert len(counted) == len(takes), (len(counted), len(takes))
        for e in sorted(counted, key=lambda e: int(e["token_atoms"])):
            t = st["takes"][e["take"]]
            assert e["account"] == t["account"] and int(e["token_atoms"]) == int(t["filled"]), (e, t)
            assert e["txid"] == f["txid"] and int(e.get("input", -1)) == int(f["k"]), e
            log("  %s: account %s bought %s for %s with round %s at input %s (take %s)" % (
                t["wallet"], e["account"][:16], e["token_atoms"], e["payment_atoms"], e["txid"][:16], e["input"], e["take"][:16]))
    if f["state"] == "out":
        assert not counted, counted
        undone = [e for e in d["purchases"] if e.get("take") in takes and e.get("undone")]
        log("  every take undone: %d purchase(s) marked undone, committed %s" % (len(undone),
            {k[:12]: v for k, v in d["committed_atoms"].items() if v}))
        for t in takes:
            acct = st["takes"][t]["account"]
            assert int(d["committed_atoms"].get(acct, 0)) == 0, (acct, d["committed_atoms"].get(acct))
    return {"states": sorted(set(got["states"].values())), "sold_atoms": sale["sold_atoms"], "buyers": sale["buyers"]}


def step_void(f):
    st = load()
    def check():
        d = ledger(st)
        t = next((t for t in d["takes"] if t["take"] == f["take"]), {})
        return (t.get("state") == "void", t)
    t = wait("take %s void" % f["take"][:16], check)
    log("take %s (%s) void: %s" % (f["take"][:16], st["takes"][f["take"]]["wallet"], t.get("reason")))
    return {"state": "void"}


def step_take_with(f):
    """One buyer takes through the sale page, in developer mode: a headless
    Chromium, a stand-in wallet whose every call the page makes is answered
    here -- signMessage with the buyer's key, takeOrder by running that
    wallet's own `arca take` -- and levod serving the built app."""
    import cdp
    import test_browser as TB
    from test_render import find_chromium
    st = load()
    label = buyer(f["wallet"])
    sec = secret(label)
    made = {}

    class Wallet:
        seen = []
        mode = "default"

        def handle(self, method, params):
            self.seen.append(method)
            if method == "connect":
                return {"connected": True}
            if method == "getCapabilities":
                return {"methods": ["connect", "signMessage", "getCapabilities", "getWalletMode", "takeOrder"], "features": []}
            if method == "getWalletMode":
                return {"mode": self.mode}
            if method == "signMessage":
                return {"signature": signhelper.sign_recoverable(sec, params["message"])}
            if method == "takeOrder":
                path = os.path.join(WORK, "page-order-%s.json" % f["wallet"])
                json.dump(params["order"], open(path, "w"))
                cmd = list(f["arca"]) + ["take", "@" + path, "--amount", str(params["amount"]), "--max-fee-ppm",
                                         str(f.get("max_fee_ppm", 50000))]
                r = subprocess.run(cmd, env=dict(os.environ, **f.get("env", {})), capture_output=True, text=True)
                log("the page's takeOrder ran %s take --amount %s: exit %d" % (f["wallet"], params["amount"], r.returncode))
                if r.returncode != 0:
                    raise RuntimeError((r.stdout or r.stderr).strip()[-400:])
                out = json.loads(r.stdout)
                made["take"] = out["take"]
                return out
            raise RuntimeError("this wallet does not support " + method)

    page = cdp.Page(find_chromium())
    try:
        page.on_new_document(TB.WALLET_JS)
        w = Wallet()
        page.go(URL + "/p/leaf-sale", settle=2.0)
        page.click("Sign in with your wallet")
        TB.wait(page, w, "!!document.querySelector('#qty')", timeout=90)
        TB.pump(page, w, times=5)
        time.sleep(1)
        assert not page.eval("!!document.querySelector('[data-leaf-amount]')"), "a leaf purchase shown outside developer mode"
        assert "from leaves" not in page.text().lower(), "the page speaks of leaves outside developer mode"
        log("signed in, the wallet in its default mode: the sale page shows no purchase from leaves and no word of them")
        w.mode = "developer"
        page.go(URL + "/p/leaf-sale", settle=2.0)
        TB.wait(page, w, "!!document.querySelector('[data-leaf-amount]')", timeout=90)
        log("the wallet in developer mode: the sale page shows 'Buy from leaves'")
        page.fill("[data-leaf-amount]", "%.8f" % (int(f["amount"]) / 1e8))
        page.click("Buy from leaves")
        TB.wait(page, w, TB.has(page, "waiting for the operator"), timeout=120)
        shown = page.eval("Array.from(document.querySelectorAll('.leaf-buy li')).map(function(l){return l.innerText}).join(' | ')")
        log("the page lists the take: %s" % shown)
        errors = page.errors()
        assert not errors, errors
        assert "getWalletMode" in w.seen and "takeOrder" in w.seen, w.seen
        assert made.get("take"), "no take made"
        st = load()
        st.setdefault("accounts", {})[label] = signhelper.pubkey_of(sec)
        st.setdefault("takes", {})[made["take"]] = {"wallet": f["wallet"], "account": signhelper.pubkey_of(sec),
                                                   "filled": int(f["amount"])}
        save(st)
        s, d = http("GET", "/api/projects/leaf-sale/takes", token=session(st, label))
        mine = [t for t in d["takes"] if t["take"] == made["take"]]
        assert mine and mine[0]["account"] == signhelper.pubkey_of(sec), d
        log("levod recorded the page's take %s against %s: %s" % (made["take"][:16], mine[0]["account"][:16], mine[0]["state"]))
        return {"take": made["take"], "wallet_calls": w.seen}
    finally:
        page.stop()


def step_end(f):
    st = load()
    pid = st.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            log("levod (pid %d) stopped" % pid)
        except ProcessLookupError:
            pass
        st["pid"] = None
        save(st)
    return {}


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        sys.stdout.write(__doc__.strip() + "\n")
        return 0 if len(sys.argv) == 2 else 2
    step = sys.argv[1]
    facts = json.loads(sys.stdin.read() or "{}")
    out = {"start": step_start, "order": step_order, "take": step_take, "take_with": step_take_with, "round": step_round,
           "void": step_void, "end": step_end}[step](facts)
    print(json.dumps(out))


if __name__ == "__main__":
    sys.exit(main())
