#!/usr/bin/env python3
"""levod -- the Levo backend.

It serves the built single-page app and the API from one origin, so a single
reverse-proxy route covers the whole platform.

What this process can and cannot do is worth being precise about, because it
determines how much trust a user has to place in whoever operates it.

It CANNOT move funds. levod holds no private keys, builds no transactions, and
signs nothing. Sale tokens sit in a covenant that only the sell and reclaim
leaves can spend, and neither leaf will accept a signature from Levo, because
neither leaf mentions Levo at all.

It CAN mislead. A hostile or broken levod could show a sale that is not really
funded, quote a price that is not really the covenant's, or hide a listing. That
is why every response that matters carries the terms needed to check it locally:
a client that rebuilds the sale address from the published terms and compares it
to the funded output does not have to trust this server about the thing that
matters most.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

import address as ADDR    # noqa: E402
import auth as A          # noqa: E402
import covenant as C      # noqa: E402
import market as M        # noqa: E402
import rails as R         # noqa: E402
import rpc as RPC         # noqa: E402
import sale as S          # noqa: E402
import store as ST        # noqa: E402
import tiers as T         # noqa: E402
import watcher as W       # noqa: E402


class App:
    """Everything the request handlers need, assembled once.

    `node` is injectable so the demo and the test drill can supply a stub. It is
    taken once and shared by every part that needs the chain, so there is no way
    to end up with half the process talking to one node and half to another.
    """

    def __init__(self, node=None):
        self.node = node or RPC.NodeRPC()
        self.links = T.StakeLinks()
        self.policy = T.TierPolicy(T.tiers_from_env())
        self.reader = T.StakeReader(self.node, self.links, self.policy)
        self.store = ST.Store()
        payment_asset = os.environ.get(
            "LEVOD_PAYMENT_ASSET",
            "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de")
        payment_label = os.environ.get("LEVOD_PAYMENT_LABEL", "USDX")
        self.hrp = os.environ.get("LEVOD_HRP", "tb")
        self.rails = R.Rails(payment_asset, payment_label,
                             R.NodeRateSource(self.node))
        self.market = M.Platform(self.store, self.reader, self.rails, self.node,
                                 hrp=self.hrp)
        self.watcher = W.Watcher(
            self.market, self.node,
            interval=int(os.environ.get("LEVOD_WATCH_SECONDS", "60")),
            hrp=self.hrp, log=lambda m: sys.stderr.write("levod %s\n" % m))
        self.challenges = A.Challenges(site="Levo")
        self.stake_challenges = A.Challenges(site="Levo")
        self.sessions = A.Sessions()
        self.webroot = Path(os.environ.get(
            "LEVOD_WEBROOT",
            str(Path(__file__).resolve().parent.parent / "web" / "dist")))


class Handler(BaseHTTPRequestHandler):
    server_version = "levod"
    app = None

    # --- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("levod %s %s\n" % (self.address_string(), fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload, indent=2, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if n > 256 * 1024:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _account(self):
        """The logged-in account, or None."""
        h = self.headers.get("Authorization") or ""
        token = h[7:].strip() if h.lower().startswith("bearer ") else None
        return self.app.sessions.verify(token) if token else None

    def _require_account(self):
        acct = self._account()
        if not acct:
            raise Unauthorised("sign in with your wallet first")
        return acct

    # --- routing ------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        try:
            if path.startswith("/api/"):
                return self._api(method, path, query)
            return self._static(path)
        except Unauthorised as e:
            self._json(401, {"error": str(e)})
        except M.NotAuthorised as e:
            self._json(403, {"error": str(e)})
        except S.CapExceeded as e:
            self._json(409, {"error": str(e), "allowance_atoms": e.allowance_atoms,
                             "enforced_by": "levo"})
        except (M.PlatformError, S.SaleError, A.BadSignature, R.RailUnavailable,
                ValueError) as e:
            self._json(400, {"error": str(e)})
        except RPC.RPCError as e:
            self._json(502, {"error": "the Sequentia node is unreachable or "
                                      "refused the query: %s" % e})
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "internal error"})

    def _api(self, method, path, query):
        app = self.app
        parts = [p for p in path.split("/") if p][1:]      # drop "api"

        # -- health and static facts ----------------------------------------
        if method == "GET" and parts == ["health"]:
            try:
                h = app.node.chain_height()
                node = {"reachable": True, "height": h}
            except Exception as e:
                node = {"reachable": False, "error": str(e)}
            return self._json(200, {"service": "levod", "node": node})

        if method == "GET" and parts == ["tiers"]:
            return self._json(200, {
                "tiers": app.policy.to_json(),
                "staking_floor_atoms": T.POS_MIN_STAKE_ATOMS,
                "note": "Tier 1 begins at the chain's own blocksigner floor of "
                        "40,000 SEQ. Only staked SEQ counts, and only for keys "
                        "you have proven you control.",
                "caps_enforced_by": "levo",
            })

        if method == "GET" and parts == ["rails"]:
            return self._json(200, {"rails": app.rails.available()})

        if method == "GET" and parts == ["watcher"]:
            w = app.watcher
            return self._json(200, {
                "running": bool(w._thread and w._thread.is_alive()),
                "interval_seconds": w.interval,
                "last_run": w.last_run,
                "last_error": w.last_error,
                "what_it_does": "reconciles every sale against the UTXO set, so "
                                "a purchase made without Levo still moves the "
                                "sale and a reorged lock stops being investable",
            })

        # -- auth -------------------------------------------------------------
        if method == "POST" and parts == ["auth", "challenge"]:
            return self._json(200, app.challenges.issue("Sign in to Levo"))

        if method == "POST" and parts == ["auth", "verify"]:
            b = self._body()
            message = b.get("message") or ""
            app.challenges.redeem(message)          # burns the nonce first
            pubkey = A.recover_pubkey(message, b.get("signature") or "")
            return self._json(200, {
                "account": pubkey,
                "token": app.sessions.issue(pubkey),
                "note": "your account is the public key that signed; Levo stores "
                        "no password and can produce no signature for you",
            })

        if method == "GET" and parts == ["me"]:
            acct = self._require_account()
            return self._json(200, app.reader.standing(acct))

        # -- linking a staking key --------------------------------------------
        if method == "POST" and parts == ["stake", "challenge"]:
            acct = self._require_account()
            b = self._body()
            staker = str(b.get("staker_pubkey") or "").lower()
            _check_pubkey(staker)
            ch = app.stake_challenges.issue()
            ch["message"] = T.StakeLinks.binding_statement(acct, staker, ch["nonce"])
            return self._json(200, ch)

        if method == "POST" and parts == ["stake", "link"]:
            acct = self._require_account()
            b = self._body()
            message = b.get("message") or ""
            staker = str(b.get("staker_pubkey") or "").lower()
            _check_pubkey(staker)
            app.stake_challenges.redeem(message)
            # The statement names both parties, so a signature proving control
            # of this key cannot be replayed onto a different account. Check the
            # NAMED FIELDS rather than comparing the whole statement verbatim:
            # the signature already covers the exact bytes, and an exact string
            # match would reject a caller whose transport trimmed a trailing
            # newline -- which shell command substitution does, silently.
            named = _fields_of(message)
            if named.get("Account") != acct:
                raise ValueError("the signed statement names account %r, but you "
                                 "are signed in as %r"
                                 % (named.get("Account"), acct))
            if named.get("Staking key") != staker:
                raise ValueError("the signed statement names staking key %r, "
                                 "not %r" % (named.get("Staking key"), staker))
            if not A.verify_signature(message, b.get("signature") or "", staker):
                raise A.BadSignature("that signature was not made by %s" % staker)
            app.links.link(acct, staker)
            app.market.save()
            return self._json(200, app.reader.standing(acct))

        if method == "POST" and parts == ["stake", "unlink"]:
            acct = self._require_account()
            b = self._body()
            app.links.unlink(acct, str(b.get("staker_pubkey") or "").lower())
            app.market.save()
            return self._json(200, app.reader.standing(acct))

        # -- projects ----------------------------------------------------------
        if method == "GET" and parts == ["projects"]:
            return self._json(200, {"projects": app.market.public_projects()})

        if method == "GET" and len(parts) == 2 and parts[0] == "projects":
            return self._json(200, app.market.project_detail(parts[1]))

        if method == "POST" and parts == ["projects"]:
            acct = self._require_account()
            b = self._body()
            p = app.market.list_project(acct, b.get("project") or {},
                                        b.get("terms") or {})
            return self._json(201, {"project": p.to_json(),
                                    "lock": app.market.lock_instructions(p)})

        if method == "POST" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "lock":
            acct = self._require_account()
            b = self._body()
            p = app.market.confirm_lock(acct, parts[1], b.get("txid"), b.get("vout"))
            return self._json(200, p.to_json())

        if method == "POST" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "buy":
            acct = self._require_account()
            b = self._body()
            plan = app.market.plan_buy(acct, parts[1],
                                       token_atoms=b.get("token_atoms"),
                                       payment_atoms=b.get("payment_atoms"))
            rail = b.get("rail") or R.USDX
            plan["quote"] = app.rails.quote(rail, plan["payment_atoms"])
            return self._json(200, plan)

        if method == "POST" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "transaction":
            acct = self._require_account()
            b = self._body()
            built = app.market.build_buy(acct, parts[1], b, b.get("buyer") or {})
            return self._json(200, built)

        if method == "POST" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "reclaim":
            acct = self._require_account()
            return self._json(200, app.market.build_reclaim(acct, parts[1], self._body()))

        if method == "POST" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "confirm":
            acct = self._require_account()
            b = self._body()
            return self._json(200, app.market.record_purchase(
                acct, parts[1], b.get("txid"),
                int(b.get("token_atoms") or 0), int(b.get("payment_atoms") or 0)))

        return self._json(404, {"error": "no such endpoint"})

    # --- the SPA ------------------------------------------------------------

    def _static(self, path):
        root = self.app.webroot
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            target = root / "index.html"      # history-API fallback
        if not target.is_file():
            return self._json(404, {"error": "the Levo web app is not built; "
                                             "run npm run build in web/"})
        body = target.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
            ".json": "application/json",
            ".png": "image/png",
            ".webmanifest": "application/manifest+json",
        }.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if target.suffix in (".js", ".css") and "/assets/" in path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class Unauthorised(Exception):
    pass


def _check_pubkey(pk):
    if len(pk) != 66 or not pk.startswith(("02", "03")) \
            or any(c not in "0123456789abcdef" for c in pk):
        raise ValueError("staker_pubkey must be a 33-byte compressed key in hex")


def _fields_of(message):
    """The `Label: value` lines of a signed statement.

    Levo's statements are written to be read by the person signing them, so
    their fields are the contract. Reading the fields back out is what lets a
    signature be checked against what it actually says.
    """
    out = {}
    for line in str(message).splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def _nonce_of(message):
    n = _fields_of(message).get("Nonce")
    if not n:
        raise ValueError("no nonce in the signed statement")
    return n


def main():
    host = os.environ.get("LEVOD_HOST", "127.0.0.1")
    port = int(os.environ.get("LEVOD_PORT", "8099"))
    Handler.app = App()
    srv = ThreadingHTTPServer((host, port), Handler)
    Handler.app.watcher.start()
    sys.stderr.write("levod listening on http://%s:%d\n" % (host, port))
    sys.stderr.write("  webroot %s\n" % Handler.app.webroot)
    sys.stderr.write("  watching the chain every %ds\n" % Handler.app.watcher.interval)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
