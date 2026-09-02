#!/usr/bin/env python3
"""levod -- the Levo backend.

It serves the built single-page app and the API from one origin, so a single
reverse-proxy route covers the whole platform.

What this process can and cannot do is worth being precise about, because it
determines how much trust a user has to place in whoever operates it.

It CANNOT move funds. levod holds no private keys and signs nothing. The
transactions it builds are unsigned, and only the buyer's (or the project's)
wallet can complete them. Sale tokens sit in a covenant that only the sell and
reclaim leaves can spend, and neither leaf will accept a signature from Levo,
because neither leaf mentions Levo at all.

It CAN mislead. A hostile or broken levod could show a sale that is not really
funded, quote a price that is not really the covenant's, or hide a listing. That
is why every response that matters carries the terms needed to check it locally:
a client that rebuilds the sale address from the published terms and compares it
to the funded output does not have to trust this server about the thing that
matters most.
"""

import json
import os
import signal
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth as A          # noqa: E402
import market as M        # noqa: E402
import rails as R         # noqa: E402
import rpc as RPC         # noqa: E402
import sale as S          # noqa: E402
import store as ST        # noqa: E402
import tiers as T         # noqa: E402
import watcher as W       # noqa: E402

MAX_BODY = 256 * 1024
WATCHER_RPC_TIMEOUT = 300         # a UTXO-set scan on a big chain takes a while
MAX_HANDLERS = 64                 # concurrent requests before the rest wait


class RateLimit:
    """A token bucket per client, for the endpoints anybody can hit.

    Issuing a challenge and checking a signature both cost the server real
    work and cost the caller nothing, so without a ceiling one client can
    evict every pending login or pin the interpreter. Clients are told apart
    by the address the reverse proxy forwards, or by the peer when there is
    no proxy.
    """

    def __init__(self, per_minute, burst=None):
        self.rate = per_minute / 60.0
        self.burst = burst or per_minute
        self._buckets = {}
        self._lock = threading.Lock()

    def allow(self, client, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            tokens, at = self._buckets.get(client, (self.burst, now))
            tokens = min(self.burst, tokens + (now - at) * self.rate)
            if tokens < 1:
                self._buckets[client] = (tokens, now)
                return False
            self._buckets[client] = (tokens - 1, now)
            if len(self._buckets) > 50_000:
                # Forget the quietest clients rather than grow without bound.
                for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[:10_000]:
                    del self._buckets[k]
            return True


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
        payment_asset = os.environ.get("LEVOD_PAYMENT_ASSET", M.DEFAULT_PAYMENT_ASSET).lower()
        payment_label = os.environ.get("LEVOD_PAYMENT_LABEL", "USDX")
        self.hrp = os.environ.get("LEVOD_HRP", "tb")
        self.chain = self._chain_name()
        self.stake_label = os.environ.get("LEVOD_STAKE_LABEL") or (
            "tSEQ" if self.chain == "test" else "SEQ")
        self.explorer_url = os.environ.get("LEVOD_EXPLORER_URL", "").rstrip("/")
        self.site_links = _site_links(os.environ.get("LEVOD_LINKS"))
        self.rails = R.Rails(payment_asset, payment_label,
                             R.NodeRateSource(self.node))
        self.watcher = None
        self.market = M.Platform(self.store, self.reader, self.rails, self.node,
                                 hrp=self.hrp, payment_asset=payment_asset,
                                 payment_label=payment_label,
                                 stake_label=self.stake_label,
                                 on_stale=lambda: self.watcher and self.watcher.nudge())
        watch_node = self.node.with_timeout(WATCHER_RPC_TIMEOUT) \
            if hasattr(self.node, "with_timeout") else self.node
        self.watcher = W.Watcher(
            self.market, watch_node,
            interval=int(os.environ.get("LEVOD_WATCH_SECONDS", "60")),
            hrp=self.hrp, log=lambda m: sys.stderr.write("levod %s\n" % m))
        self.challenges = A.Challenges(site="Levo")
        self.stake_challenges = A.Challenges(site="Levo")
        self.sessions = A.Sessions()
        self.auth_limit = RateLimit(per_minute=int(os.environ.get("LEVOD_AUTH_PER_MINUTE", "30")))
        self.write_limit = RateLimit(per_minute=int(os.environ.get("LEVOD_WRITES_PER_MINUTE", "120")))
        self.handlers = threading.BoundedSemaphore(MAX_HANDLERS)
        self.webroot = Path(os.environ.get(
            "LEVOD_WEBROOT",
            str(Path(__file__).resolve().parent.parent / "web" / "dist")))
        self.verbose = bool(os.environ.get("LEVOD_VERBOSE"))

    def _chain_name(self):
        try:
            return self.node.chain_name()
        except Exception:
            return os.environ.get("LEVOD_CHAIN", "")

    def config(self):
        """The facts a client needs to label and link things correctly."""
        tiers = self.policy.tiers
        first = tiers[1] if len(tiers) > 1 else tiers[0]
        return {
            "chain": self.chain,
            "hrp": self.hrp,
            "testnet": self.chain != "sequentia",
            "explorer_url": self.explorer_url,
            "payment": {"asset": self.rails.payment_asset,
                        "label": self.rails.payment_label, "decimals": 8},
            "stake": {"label": self.stake_label, "decimals": 8},
            "staking_floor_atoms": T.POS_MIN_STAKE_ATOMS,
            "first_tier_atoms": first.min_stake_atoms,
            "first_tier_is_chain_floor": first.min_stake_atoms == T.POS_MIN_STAKE_ATOMS,
            "links": self.site_links,
        }

    def tiers_note(self):
        first = self.policy.tiers[1] if len(self.policy.tiers) > 1 else None
        if first is None:
            return "Only staked Sequence counts, and only for keys you have proven you control."
        text = "The first tier begins at %s staked." % _seq(first.min_stake_atoms, self.stake_label)
        if first.min_stake_atoms == T.POS_MIN_STAKE_ATOMS:
            text += (" That is the chain's own blocksigner floor: below it, "
                     "consensus ignores a staker's weight entirely.")
        else:
            text += (" The chain's own blocksigner floor is %s."
                     % _seq(T.POS_MIN_STAKE_ATOMS, self.stake_label))
        return text + (" Only staked Sequence counts, and only for keys you have "
                       "proven you control.")


class Handler(BaseHTTPRequestHandler):
    server_version = "levod"
    sys_version = ""
    timeout = 30                       # a client that stops sending frees its thread
    app = None

    def version_string(self):
        return "levod"                 # no interpreter version on the wire

    def client(self):
        """Who is asking: the address the proxy forwards when the peer is the
        proxy, otherwise the peer itself."""
        peer = self.client_address[0] if self.client_address else "?"
        if peer in ("127.0.0.1", "::1"):
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return peer

    def handle_one_request(self):
        acquired = self.app.handlers.acquire(timeout=30) if self.app else True
        try:
            if not acquired:
                self._json(503, {"error": "levod is busy; try again in a moment"})
                return
            super().handle_one_request()
        finally:
            if acquired and self.app:
                self.app.handlers.release()

    # --- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):
        if not self.app or not self.app.verbose:
            # Health checks, tier tables and asset fetches say nothing when
            # they succeed; the journal is for what an operator acts on.
            path = urlparse(self.path).path
            code = str(args[1]) if len(args) > 1 else ""
            quiet = path in ("/api/health", "/api/tiers", "/api/rails", "/api/config") \
                or path.startswith("/assets/") or path.endswith((".svg", ".png", ".woff2"))
            if quiet and code.startswith("2"):
                return
        sys.stderr.write("levod %s %s\n" % (self.client(), fmt % args))

    def log_error(self, fmt, *args):
        pass                            # the request line is logged once, above

    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline'; font-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass                    # the client left; nothing to report

    def _json(self, code, payload):
        body = json.dumps(payload, indent=2, sort_keys=True).encode()
        self._send(code, body, "application/json")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise Unsupported(411, "send the request body with a Content-Length")
        raw = self.headers.get("Content-Length") or "0"
        try:
            n = int(raw)
        except ValueError:
            raise ValueError("Content-Length must be a number")
        if n < 0:
            raise ValueError("Content-Length must be a number, 0 or more")
        if n > MAX_BODY:
            raise ValueError("the request body is larger than %d bytes" % MAX_BODY)
        if not n:
            return {}
        data = self.rfile.read(n)
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("the request body is not valid JSON")
        if not isinstance(body, dict):
            raise ValueError("the request body must be a JSON object")
        return body

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

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        self._method_not_allowed()

    def _method_not_allowed(self):
        body = json.dumps({"error": "method not allowed"}).encode()
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self, method):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api" or path.startswith("/api/"):
                return self._api(method, path, query)
            if method != "GET":
                return self._method_not_allowed()
            return self._static(path)
        except Unsupported as e:
            self._json(e.code, {"error": str(e)})
        except Unauthorised as e:
            self._json(401, {"error": str(e)})
        except M.NotFound as e:
            self._json(404, {"error": str(e)})
        except M.NotAuthorised as e:
            self._json(403, {"error": str(e)})
        except S.CapExceeded as e:
            self._json(409, {"error": str(e), "allowance_atoms": e.allowance_atoms,
                             "enforced_by": "levo"})
        except (M.PlatformError, S.SaleError, A.BadSignature, R.RailUnavailable,
                ValueError) as e:
            self._json(400, {"error": str(e)})
        except (KeyError, TypeError, AttributeError) as e:
            # A field of the wrong shape. The request is at fault, not the
            # server, and the caller deserves to hear which field.
            self._json(400, {"error": "malformed request: %s" % _describe(e)})
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
                node = {"reachable": True, "height": h, "chain": app.chain}
            except Exception as e:
                node = {"reachable": False, "error": str(e), "chain": app.chain}
            w = app.watcher
            age = (time.time() - w.last_run) if w.last_run else None
            stale = bool(w._thread) and (age is None or age > 3 * w.interval)
            ok = node["reachable"] and not stale
            return self._json(200 if ok else 503, {
                "service": "levod", "ok": ok, "node": node,
                "watcher": {"running": bool(w._thread and w._thread.is_alive()),
                            "last_run_age_seconds": int(age) if age is not None else None,
                            "last_error": w.last_error},
            })

        if method == "GET" and parts == ["config"]:
            return self._json(200, app.config())

        if method == "GET" and parts == ["tiers"]:
            return self._json(200, {
                "tiers": app.policy.to_json(),
                "staking_floor_atoms": T.POS_MIN_STAKE_ATOMS,
                "stake_label": app.stake_label,
                "note": app.tiers_note(),
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
        if method == "POST" and parts[:1] in (["auth"], ["stake"]) and \
                not app.auth_limit.allow(self.client()):
            raise Unsupported(429, "too many sign-in attempts from this address; "
                                   "wait a minute and try again")
        if method in ("POST", "PATCH", "DELETE") and parts[:1] == ["projects"] and \
                not app.write_limit.allow(self.client()):
            raise Unsupported(429, "too many requests from this address; wait a "
                                   "minute and try again")

        if method == "POST" and parts == ["auth", "challenge"]:
            return self._json(200, app.challenges.issue("Sign in to Levo"))

        if method == "POST" and parts == ["auth", "verify"]:
            b = self._body()
            message = _str(b, "message")
            app.challenges.redeem(message)          # burns the nonce first
            pubkey = A.recover_pubkey(message, _str(b, "signature"))
            address = b.get("address")
            if address:
                # The caller names the address it signed with, so a signature
                # over slightly different bytes is a clear error rather than a
                # phantom account nobody controls.
                if not A.key_matches_address(pubkey, str(address)):
                    raise A.BadSignature(
                        "that signature was not made by the key behind %s. The "
                        "text that was signed differs from the challenge, or the "
                        "wallet signed with another key" % address)
            return self._json(200, {
                "account": pubkey,
                "token": app.sessions.issue(pubkey),
                "note": "your account is the public key that signed; Levo stores "
                        "no password and can produce no signature for you",
            })

        if method == "GET" and parts == ["me"]:
            acct = self._require_account()
            standing = app.reader.standing(acct)
            if app.links.dirty:
                with app.market.lock:
                    app.links.dirty = False
                    app.market.save()
            return self._json(200, standing)

        if method == "GET" and parts == ["me", "projects"]:
            acct = self._require_account()
            return self._json(200, {"projects": app.market.projects_of(acct)})

        if method == "GET" and parts == ["me", "positions"]:
            acct = self._require_account()
            standing = app.reader.standing(acct)
            tier = app.policy.for_stake(standing["stake_atoms"])
            return self._json(200, {"positions": app.market.positions(acct, tier),
                                    "tier": tier.to_json()})

        # -- linking a staking key --------------------------------------------
        if method == "POST" and parts == ["stake", "challenge"]:
            acct = self._require_account()
            b = self._body()
            staker = _str(b, "staker_pubkey").lower()
            _check_pubkey(staker)
            ch = app.stake_challenges.issue(
                purpose=T.StakeLinks.PURPOSE,
                extra_lines=T.StakeLinks.binding_lines(acct, staker))
            return self._json(200, ch)

        if method == "POST" and parts == ["stake", "link"]:
            acct = self._require_account()
            b = self._body()
            message = _str(b, "message")
            staker = _str(b, "staker_pubkey").lower()
            _check_pubkey(staker)
            app.stake_challenges.redeem(message)
            # The statement names both parties, so a signature proving control
            # of this key cannot be replayed onto a different account. Check the
            # NAMED FIELDS as well as the text: they are the contract.
            named = _fields_of(message)
            if named.get("Account") != acct:
                raise ValueError("the signed statement names account %r, but you "
                                 "are signed in as %r"
                                 % (named.get("Account"), acct))
            if named.get("Staking key") != staker:
                raise ValueError("the signed statement names staking key %r, "
                                 "not %r" % (named.get("Staking key"), staker))
            if not A.verify_signature(message, _str(b, "signature"), staker):
                raise A.BadSignature("that signature was not made by %s; the wallet "
                                     "signed with a different key" % staker)
            with app.market.lock:
                app.links.link(acct, staker)
                app.market.save()
            return self._json(200, app.reader.standing(acct))

        if method == "POST" and parts == ["stake", "unlink"]:
            acct = self._require_account()
            b = self._body()
            staker = _str(b, "staker_pubkey").lower()
            _check_pubkey(staker)
            if staker not in app.links.keys_for(acct):
                raise M.NotFound("that staking key is not linked to your account")
            with app.market.lock:
                app.links.unlink(acct, staker)
                app.market.save()
            return self._json(200, app.reader.standing(acct))

        # -- projects ----------------------------------------------------------
        if method == "GET" and parts == ["projects"]:
            return self._json(200, {"projects": app.market.public_projects(),
                                    "node_reachable": app.market.height() is not None})

        if len(parts) == 2 and parts[0] == "projects":
            slug = parts[1]
            if method == "GET":
                d = app.market.project_detail(slug)
                d["node_reachable"] = app.market.height() is not None
                return self._json(200, d)
            if method == "PATCH":
                acct = self._require_account()
                p = app.market.update_project(acct, slug, self._body())
                return self._json(200, app.market.project_detail(p.slug))
            if method == "DELETE":
                acct = self._require_account()
                app.market.withdraw(acct, slug)
                return self._json(200, {"withdrawn": slug})

        if method == "POST" and parts == ["projects"]:
            acct = self._require_account()
            b = self._body()
            p = app.market.list_project(acct, b.get("project") or {},
                                        b.get("terms") or {})
            return self._json(201, {"project": p.to_json(),
                                    "lock": app.market.lock_instructions(p)})

        if method == "POST" and len(parts) == 3 and parts[0] == "projects":
            slug, action = parts[1], parts[2]
            if action == "lock":
                acct = self._require_account()
                b = self._body()
                p = app.market.confirm_lock(acct, slug, b.get("txid"), b.get("vout"))
                return self._json(200, p.to_json(height=app.market.height()))
            if action == "withdraw":
                acct = self._require_account()
                app.market.withdraw(acct, slug)
                return self._json(200, {"withdrawn": slug})
            if action == "buy":
                acct = self._require_account()
                b = self._body()
                plan = app.market.plan_buy(acct, slug,
                                           token_atoms=b.get("token_atoms"),
                                           payment_atoms=b.get("payment_atoms"))
                plan["quote"] = app.rails.quote(b.get("rail") or R.USDX, plan["payment_atoms"])
                return self._json(200, plan)
            if action == "transaction":
                acct = self._require_account()
                b = self._body()
                built = app.market.build_buy(acct, slug, b, b.get("buyer") or {})
                return self._json(200, built)
            if action == "reclaim":
                acct = self._require_account()
                return self._json(200, app.market.build_reclaim(acct, slug, self._body()))
            if action == "confirm":
                acct = self._require_account()
                b = self._body()
                recorded = app.market.record_purchase(
                    acct, slug, b.get("txid"), b.get("token_atoms"), b.get("payment_atoms"))
                return self._json(200, recorded)

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
            if rel.startswith("assets/") or rel.startswith("fonts/") \
                    or (target.suffix and target.suffix != ".html"):
                # A file that is not there is not the app: a stale bundle
                # asking for an old hashed asset should hear 404 and reload,
                # not receive HTML as a script.
                return self._json(404, {"error": "no such file"})
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
            ".woff2": "font/woff2",
            ".webmanifest": "application/manifest+json",
        }.get(target.suffix, "application/octet-stream")
        if target.suffix in (".js", ".css", ".woff2") and ("/assets/" in path or "/fonts/" in path):
            cache = "public, max-age=31536000, immutable"
        else:
            cache = "no-cache"
        self._send(200, body, ctype, cache=cache)


class Unauthorised(Exception):
    pass


class Unsupported(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _check_pubkey(pk):
    if len(pk) != 66 or not pk.startswith(("02", "03")) \
            or any(c not in "0123456789abcdef" for c in pk):
        raise ValueError("staker_pubkey must be a 33-byte compressed key in hex")


def _str(body, name):
    v = body.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ValueError("%s is required" % name)
    return v


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


def _describe(e):
    if isinstance(e, KeyError):
        return "missing field %s" % e
    return str(e) or e.__class__.__name__


def _seq(atoms, label):
    import units as U
    return U.fmt(atoms, 8, label)


def _site_links(raw):
    """Where the rest of this deployment lives (wallet, faucet, staking pools),
    as JSON in LEVOD_LINKS. Only http(s) URLs are kept."""
    if not raw:
        return {}
    try:
        return M.validate_links(json.loads(raw))
    except (ValueError, M.PlatformError) as e:
        sys.stderr.write("levod: LEVOD_LINKS ignored: %s\n" % e)
        return {}


def main():
    host = os.environ.get("LEVOD_HOST", "127.0.0.1")
    port = int(os.environ.get("LEVOD_PORT", "8099"))
    Handler.app = App()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    Handler.app.watcher.start()
    sys.stderr.write("levod listening on http://%s:%d\n" % (host, port))
    sys.stderr.write("  chain %s, webroot %s\n" % (Handler.app.chain or "unknown", Handler.app.webroot))
    sys.stderr.write("  watching the chain every %ds\n" % Handler.app.watcher.interval)

    def stop(signum, frame):
        # systemd stops with SIGTERM. Let a save in flight finish, stop the
        # watcher, and close the listener, rather than dying mid-write.
        Handler.app.watcher.stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with Handler.app.market.lock:
            pass                        # wait for any mutation to finish
        srv.server_close()


if __name__ == "__main__":
    main()
