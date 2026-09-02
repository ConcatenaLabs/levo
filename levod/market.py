"""The Levo marketplace: projects, listings, and the rules around a sale.

This is the layer that decides who may do what. The rules it enforces are:

  * Only a tier that may list can create a project. Listing is the one action
    Levo gates on tier rather than merely sizing by it.
  * A project is a draft until its tokens are locked and the lock verifies
    against the terms. Levo will not display an unlocked sale as investable.
  * Every purchase is planned against the buyer's remaining allowance for that
    sale, and the ledger of what each account has committed is kept here.

Everything below this layer is chain truth; everything at this layer is Levo
policy. Keeping that boundary sharp is what lets the documentation say exactly
which promises survive Levo going away.
"""

import re
import threading
import time
from urllib.parse import urlparse

import address as ADDR
import covenant as C
import rpc as RPCMOD
import sale as S
import tx as TX
import units as U


class PlatformError(ValueError):
    pass


class NotAuthorised(PlatformError):
    pass


class NotFound(PlatformError):
    pass


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
TXID_RE = re.compile(r"^[0-9a-f]{64}$")
ASSET_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_LINKS = 8
MAX_INPUTS = 32                  # more than any wallet needs for one purchase
# How many of a claimed purchase's outputs are looked at when asking whether
# the node has ever heard of it. A buy pays the treasury, re-rests the
# remainder, and delivers the buyer's tokens, so anything real is in the first
# few.
OUTPUTS_LOOKED_AT = 4
# Measured against the builder's own vsize for a buy (one covenant input, the
# treasury credit, the remainder, the buyer's tokens, change and the fee) and
# for a reclaim, rounded up so an advised fee is never under the relay floor.
BUY_BASE_VSIZE = 490
RECLAIM_BASE_VSIZE = 300
PER_INPUT_VSIZE = 70

SALE_FILTERS = ("all", "open", "draft", "finished")
SALE_SORTS = ("new", "closing", "progress")
DEFAULT_PAGE = 50                # listings a request returns when it asks for no size
MAX_PAGE = 200                   # the most one request will return
MAX_STRAYS_KEPT = 20             # foreign outputs kept when a state file is read
PURCHASES_SHOWN = 20             # purchases carried inline with a position
MAX_PURCHASES_PER_ACCOUNT = 64   # ledger entries one account keeps per sale
MAX_DRAFTS = 3                   # unfunded listings one account may hold at once
DEFAULT_PAYMENT_ASSET = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"


def validate_links(links):
    """A short map of label to absolute http(s) URL, or a refusal.

    Links are rendered as anchors on a public page, so anything that is not a
    web URL is refused here rather than trusted later.
    """
    if links is None:
        return {}
    if not isinstance(links, dict):
        raise PlatformError("links must be an object of label: URL")
    out = {}
    for k, v in links.items():
        label = str(k).strip()
        if not label or len(label) > 24:
            raise PlatformError("each link label is 1 to 24 characters")
        if not isinstance(v, str):
            raise PlatformError("link %r must be a URL" % label)
        url = v.strip()
        if not url:
            continue
        if len(url) > 200:
            raise PlatformError("link %r is longer than 200 characters" % label)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PlatformError("link %r must be an http or https URL" % label)
        out[label] = url
    if len(out) > MAX_LINKS:
        raise PlatformError("a project carries at most %d links" % MAX_LINKS)
    return out


def _text(v, name, limit, required=False):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise PlatformError("%s must be text" % name)
    v = v.strip()
    if required and not v:
        raise PlatformError("a project needs a %s" % name)
    if len(v) > limit:
        raise PlatformError("the %s is limited to %d characters" % (name, limit))
    return v


def _decimals(v):
    if v is None:
        return 8
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 8:
        raise PlatformError("decimals is the number of decimal places the token "
                            "is shown with, 0 to 8")
    return v


class Project:
    """A listing: what it is, who runs it, and the sale attached to it."""

    def __init__(self, slug, name, ticker, summary, description, issuer_account,
                 links=None, created_at=None, decimals=8):
        if not isinstance(slug, str) or not SLUG_RE.match(slug or ""):
            raise PlatformError("the page name must be 3 to 40 lowercase letters, "
                                "digits or hyphens, starting and ending with a "
                                "letter or digit")
        if not isinstance(ticker, str) or not re.match(r"^[A-Z0-9]{2,12}$", ticker or ""):
            raise PlatformError("the ticker must be 2 to 12 uppercase letters or digits")
        self.slug = slug
        self.name = _text(name, "name", 80, required=True)
        self.ticker = ticker
        self.summary = _text(summary, "summary", 200)
        self.description = _text(description, "description", 8000)
        self.issuer_account = issuer_account
        self.links = validate_links(links)
        self.decimals = _decimals(decimals)
        self.created_at = created_at or int(time.time())
        self.sale = None
        # Set by whoever runs this Levo, never by the project. A listing that
        # turns out to be a fraud has to be able to stop being advertised
        # without anybody editing a state file by hand -- and the sale itself
        # carries on regardless, because it is a covenant on a public chain
        # and Levo has no power over it.
        self.hidden = False
        self.notice = None

    def update(self, meta):
        """Change what a project says about itself. The terms and the page
        name are not here: they are the sale, and the sale is the address."""
        if not isinstance(meta, dict):
            raise PlatformError("send the fields to change as an object")
        if "name" in meta:
            self.name = _text(meta.get("name"), "name", 80, required=True)
        if "summary" in meta:
            self.summary = _text(meta.get("summary"), "summary", 200)
        if "description" in meta:
            self.description = _text(meta.get("description"), "description", 8000)
        if "links" in meta:
            self.links = validate_links(meta.get("links"))
        if "decimals" in meta and _decimals(meta.get("decimals")) != self.decimals:
            # Every amount in the terms is in atoms, and the decimals are how
            # they are read: the same 100000000000 is 1,000 tokens at eight
            # decimals and 1,000,000,000 at two. Changing them after a sale
            # exists reprices the whole page without touching a single number
            # the covenant sees, so it is not an edit -- it is a different
            # sale. A draft can be withdrawn and listed again in a moment.
            raise PlatformError(
                "the token's decimals cannot change once a sale exists: every "
                "amount in its terms is in atoms, and the decimals are how those "
                "amounts are read, so changing them would reprice the sale "
                "without changing the covenant. Withdraw this listing and list "
                "it again")

    def to_json(self, height=None, now=None):
        return {
            "slug": self.slug,
            "name": self.name,
            "ticker": self.ticker,
            "summary": self.summary,
            "description": self.description,
            "issuer_account": self.issuer_account,
            "links": dict(self.links),
            "decimals": self.decimals,
            "created_at": self.created_at,
            "hidden": self.hidden,
            "notice": self.notice,
            "sale": self.sale.to_json(height=height, now=now) if self.sale else None,
        }


class Platform:
    def __init__(self, store, stake_reader, rails=None, rpc=None, hrp="tb",
                 payment_asset=None, payment_label=None, stake_label="SEQ",
                 on_stale=None, operators=(), payment_decimals=8):
        self.store = store
        self.stake = stake_reader
        self.rails = rails
        self.rpc = rpc
        # Either the prefix itself or something that answers with it. A levod
        # whose node was down at startup does not know its chain yet, and the
        # answer must not be frozen at that moment.
        self._hrp = hrp
        self.payment_asset = (payment_asset or (rails.payment_asset if rails else None)
                              or DEFAULT_PAYMENT_ASSET).lower()
        self.payment_label = payment_label or (rails.payment_label if rails else None) or "USDX"
        # How many places the payment asset divides into. Every message that
        # quotes a price or a cap reads atoms through it.
        self.payment_decimals = int(8 if payment_decimals is None else payment_decimals)
        self.stake_label = stake_label
        # Accounts that may flag or hide a listing on this Levo. Empty by
        # default: nobody has that power unless the operator grants it to a
        # named key.
        self.operators = {str(a).lower() for a in (operators or ()) if a}
        # Called when a purchase or lock changes what the chain holds, so the
        # watcher can look now rather than at its next interval.
        self.on_stale = on_stale or (lambda: None)
        self.projects = {}
        # The watcher thread and the request threads all read and write the
        # same listings. One lock around every mutation and every save keeps a
        # poll from rewriting a sale halfway through a purchase being recorded,
        # and keeps the state file from being serialised mid-change.
        self.lock = threading.RLock()
        self._load()

    # --- persistence --------------------------------------------------------

    def _make_sale(self, p, terms, created_at=None):
        return S.Sale(p.slug, terms, p.issuer_account, created_at,
                      token_label=p.ticker, token_decimals=p.decimals,
                      payment_label=self.payment_label,
                      payment_decimals=self.payment_decimals)

    def _load(self):
        """Read the state file, or refuse to start.

        A file that parses as JSON can still be damaged: a sale without its
        terms, a number where an object belongs, a truncated write from a full
        disk. Letting that raise here gives systemd a crash loop and no
        message. The store's refusal exits with a status that says restarting
        cannot help, and names the listing that could not be read.
        """
        try:
            self._read_projects()
        except SystemExit:
            raise
        except Exception as e:
            self.store._refuse(
                "the state file %s parses but cannot be read: %s. Restore it "
                "from a backup rather than starting with an empty ledger."
                % (self.store.path, e))

    def _read_projects(self):
        for slug, d in (self.store.data.get("projects") or {}).items():
            p = Project(d["slug"], d["name"], d["ticker"], d.get("summary"),
                        d.get("description"), d["issuer_account"],
                        d.get("links"), d.get("created_at"), d.get("decimals", 8))
            p.hidden = bool(d.get("hidden"))
            p.notice = d.get("notice")
            sd = d.get("sale")
            if sd:
                terms = C.SaleTerms.from_json(sd["terms"])
                sl = self._make_sale(p, terms, sd.get("created_at"))
                sl.status = sd.get("status", S.DRAFT)
                sl.funding = sd.get("funding")
                sl.locked_atoms = int(sd.get("locked_atoms", 0))
                sl.sold_atoms = int(sd.get("sold_atoms", 0))
                sl.allocations = {k: int(v) for k, v in (sd.get("allocations") or {}).items()}
                sl.purchases = {k: list(v) for k, v in (sd.get("purchases") or {}).items()}
                sl.candidates = list(sd.get("candidates") or [])
                sl.reclaim_txids = list(sd.get("reclaim_txids") or [])
                sl.strays = list(sd.get("strays") or [])[:MAX_STRAYS_KEPT]
                # The address is what everything about a sale rests on: the
                # watcher reads it, buyers pay it, and the reclaim spends it.
                # If the terms in this file derive an address other than the
                # one they derived when the sale was funded -- a rollback to a
                # levod that serialised them differently, a hand edit -- then
                # levod would quietly watch, quote and reclaim the wrong one.
                was = sd.get("script_pubkey")
                if was and was != sl.script_pubkey:
                    self.store._refuse(
                        "the sale %s no longer derives the address it was "
                        "funded at: the file says %s and its terms make %s. "
                        "Restore the file from a backup taken by the version "
                        "that wrote it." % (slug, was, sl.script_pubkey))
                p.sale = sl
            self.projects[slug] = p
        self.stake.links.load(self.store.data.get("stake_links"))

    def save(self):
        """Write the platform to disk.

        The lock is held while the state is assembled and dropped before the
        bytes go to the disk. A save with a thousand listings on a busy disk is
        the longest thing levod does, and holding the lock across it would park
        every purchase, lock and listing behind an fsync.
        """
        with self.lock:
            out = {}
            for slug, p in self.projects.items():
                d = p.to_json()
                if p.sale:
                    d["sale"] = p.sale.to_json()
                    d["sale"]["allocations"] = dict(p.sale.allocations)
                    d["sale"]["purchases"] = {k: list(v) for k, v in p.sale.purchases.items()}
                    d["sale"]["candidates"] = list(p.sale.candidates)
                    d["sale"]["strays"] = list(p.sale.strays)
                    d["sale"]["created_at"] = p.sale.created_at
                    # Written so a later load can check the terms still derive
                    # it. Nothing reads it but that check.
                    d["sale"]["script_pubkey"] = p.sale.script_pubkey
                    # `status` is recomputed on read for closure; persist the raw one.
                    d["sale"]["status"] = p.sale.status
                out[slug] = d
            self.store.data["projects"] = out
            self.store.data["stake_links"] = self.stake.links.to_json()
            payload = self.store.snapshot()
        self.store.write(payload)

    # --- chain context ------------------------------------------------------

    def height(self, strict=False):
        """The chain's height, or None if the node cannot be asked.

        `strict` turns silence into a refusal, for the paths where an unknown
        height would otherwise pass as 'not closed': planning a buy, listing a
        sale that closes at a height, building a reclaim.
        """
        try:
            return self.rpc.chain_height() if self.rpc else None
        except Exception:
            if strict:
                raise PlatformError("the Sequentia node is unreachable, so the "
                                    "chain height cannot be checked; try again shortly")
            return None

    def median_time(self):
        try:
            return self.rpc.median_time() if self.rpc else None
        except Exception:
            return None

    # --- listing ------------------------------------------------------------

    def list_project(self, account, meta, terms_json):
        """Create a listing. Only a tier that may list can do this."""
        with self.lock:
            return self._list_project(account, meta, terms_json)

    def _list_project(self, account, meta, terms_json):
        if not isinstance(meta, dict):
            raise PlatformError("project must be an object")
        if not isinstance(terms_json, dict):
            raise PlatformError("terms must be an object")
        standing = self.stake.standing(account)
        if not standing["tier"]["may_list"]:
            lt = self.stake.policy.listing_tier()
            raise NotAuthorised(
                "listing a project needs the %s tier (%s staked); you are %s "
                "with %s staked" % (
                    lt.name, U.fmt(lt.min_stake_atoms, 8, self.stake_label),
                    standing["tier"]["name"],
                    U.fmt(standing["stake_atoms"], 8, self.stake_label)))
        ticker = str(meta.get("ticker") or "").upper()
        if ticker and ticker == (self.payment_label or "").upper():
            raise PlatformError(
                "%s is what this Levo prices sales in, so a token cannot use it "
                "as its ticker: every amount on the board would read as that "
                "asset" % self.payment_label)
        slug = meta.get("slug")
        if slug in self.projects:
            raise PlatformError("a project with that page name already exists")
        drafts = [p for p in self.projects.values()
                  if p.issuer_account == account and p.sale
                  and p.sale.status in (S.DRAFT, S.GHOST)]
        if len(drafts) >= MAX_DRAFTS:
            raise PlatformError(
                "you already have %d listings waiting to be funded (%s); lock or "
                "withdraw one before listing another"
                % (len(drafts), ", ".join(sorted(d.slug for d in drafts))))
        p = Project(slug, meta.get("name"), meta.get("ticker"), meta.get("summary"),
                    meta.get("description"), account, meta.get("links"),
                    decimals=meta.get("decimals", 8))
        terms_json = dict(terms_json)
        for k in ("token_asset", "price_num", "price_den", "min_lot",
                  "close_locktime", "reclaim_xonly", "total_atoms"):
            if terms_json.get(k) is None:
                raise PlatformError("the terms are missing %s" % k)
        if terms_json.get("treasury_prog") is None and not terms_json.get("treasury_address"):
            raise PlatformError("the terms are missing the treasury: give "
                                "treasury_address (the address payments should "
                                "land at) or treasury_prog")
        # The treasury is normally given as the address a wallet shows rather
        # than as the witness program the leaf pins. Either version is taken:
        # taproot, or the version-0 address most wallets hand out.
        if terms_json.get("treasury_address"):
            try:
                ver, prog = ADDR.witness_program(
                    terms_json["treasury_address"], self.hrp)
            except ValueError as e:
                raise PlatformError("treasury address: %s" % e)
            terms_json["treasury_prog"] = prog
            terms_json["treasury_ver"] = ver
        # Every sale on this Levo is priced in the one asset it quotes and
        # checks fees in. A sale in another asset would be listed, then fail
        # to be quoted, paid or capped correctly.
        pa = str(terms_json.get("payment_asset") or self.payment_asset).lower()
        if pa != self.payment_asset:
            raise PlatformError(
                "sales here are priced in %s (%s); this listing names a different "
                "payment asset, which Levo could neither quote nor cap"
                % (self.payment_label, self.payment_asset))
        terms_json["payment_asset"] = pa
        # Canonicalise the price before anything is derived from it: the same
        # ratio in lowest terms buys a large amount of overflow headroom, and
        # after funding it is far too late to change.
        submitted = (terms_json.get("price_num"), terms_json.get("price_den"))
        if submitted[0] and submitted[1]:
            try:
                terms_json["price_num"], terms_json["price_den"] = C.canonical_price(*submitted)
            except (TypeError, ValueError) as e:
                raise PlatformError("price: %s" % e)
        try:
            terms = C.SaleTerms.from_json(terms_json)
        except ValueError as e:
            raise PlatformError(str(e))
        # A taproot treasury program is an output key: the x coordinate of a
        # point. Not every 32-byte number is one, and a treasury that is not a
        # point can be paid and never spent -- every buyer's payment would land
        # somewhere nobody can reach. An address from a wallet always gives a
        # point; a raw treasury_prog need not, so it is checked here.
        if terms.treasury_ver == 1 and \
                C.EC.lift_x(int(terms.treasury_prog, 16)) is None:
            raise PlatformError(
                "that taproot treasury is not a point on the curve, so anything "
                "paid to it could never be spent. Give treasury_address and let "
                "Levo take the program from it")
        # A sale that has already closed can never be bought, only reclaimed, so
        # listing one is always a mistake. The close is an absolute locktime:
        # below 500000000 it is a HEIGHT and above it a unix time, and both need
        # checking against the right clock.
        if terms.close_locktime >= 500_000_000:
            if terms.close_locktime <= time.time():
                raise PlatformError("the sale's close time is already in the past")
        else:
            h = self.height(strict=True)
            if h is not None and terms.close_locktime <= h:
                raise PlatformError(
                    "the sale closes at block %d and the chain is already at "
                    "%d, so it would be closed the moment it was listed"
                    % (terms.close_locktime, h))
        sale = self._make_sale(p, terms)
        # Identical terms derive an identical covenant address, and the address
        # is the whole of what a lock is proven against. Two listings sharing
        # one would let the second adopt the first's locked outpoint and show
        # tokens it does not have, so the second is refused instead.
        clash = next((q for q in self.projects.values()
                      if q.sale and q.sale.script_pubkey == sale.script_pubkey), None)
        if clash is not None:
            raise PlatformError(
                "these terms derive the same sale address as the listing %r, so "
                "the two sales could not be told apart on chain. Change something "
                "the address is made of -- the price, the amount, the minimum "
                "lot, the close, the treasury or the reclaim key" % clash.slug)
        p.sale = sale
        p.price_was_reduced = (submitted[0], submitted[1]) != (terms.price_num, terms.price_den)
        self.projects[slug] = p
        self.save()
        return p

    def withdraw(self, account, slug):
        """Remove a listing that was never funded. A funded sale is the chain's;
        after the close it is reclaimed, not withdrawn."""
        with self.lock:
            p = self._project(slug)
            if p.issuer_account != account:
                raise NotAuthorised("only the project's issuer can withdraw it")
            if p.sale and (p.sale.funding or p.sale.status not in (S.DRAFT, S.GHOST)):
                raise PlatformError("a funded sale cannot be withdrawn; after the close, "
                                    "reclaim what did not sell")
            del self.projects[slug]
            self.save()

    def update_project(self, account, slug, meta):
        with self.lock:
            p = self._project(slug)
            if p.issuer_account != account:
                raise NotAuthorised("only the project's issuer can edit it")
            p.update(meta)
            self.save()
            return p

    def confirm_lock(self, account, slug, txid=None, vout=None):
        """Verify on chain that the project really locked the tokens.

        Levo reads the output itself rather than believing the issuer's claim,
        and the scriptPubKey it finds must equal the one the published terms
        derive. That equality is the entire trust argument for the sale.

        Without an outpoint, the confirmed UTXO set is scanned for the sale's
        address, so an issuer who sent the tokens from a wallet that does not
        show output indexes can still confirm.
        """
        with self.lock:
            return self._confirm_lock(account, slug, txid, vout)

    def _confirm_lock(self, account, slug, txid, vout):
        p = self._project(slug)
        if p.issuer_account != account:
            raise NotAuthorised("only the project's issuer can confirm its lock")
        if p.sale is None:
            raise PlatformError("this project has no sale")
        if self.rpc is None:
            raise PlatformError("no node connection; cannot verify the lock")
        if not txid:
            txid, vout = self._find_lock(p.sale)
        txid = str(txid).lower()
        if not TXID_RE.match(txid):
            raise PlatformError("txid must be 64 hex characters")
        if isinstance(vout, bool) or not isinstance(vout, int) or vout < 0:
            raise PlatformError("vout is the output's index in the transaction, 0 or more")
        out = self.rpc.txout(txid, vout)
        if out is None:
            raise PlatformError(
                "no unspent output at %s:%s -- it does not exist, it has "
                "already been spent, or the node has not seen it yet" % (txid, vout))
        spk = (out.get("scriptPubKey") or {}).get("hex")
        asset = out.get("asset") or out.get("assetlabel")
        value = _value_atoms(out)
        blinded = bool(out.get("valuecommitment") or out.get("amountcommitment")
                       or out.get("assetcommitment"))
        taken = next((q for q in self.projects.values()
                      if q is not p and q.sale and q.sale.funding
                      and q.sale.funding.get("txid") == txid
                      and q.sale.funding.get("vout") == vout), None)
        if taken is not None:
            raise PlatformError(
                "that outpoint is already the lock of the sale %r; one output "
                "funds one sale" % taken.slug)
        p.sale.confirm_lock(txid, vout, spk, value, asset, blinded=blinded)
        self._date_funding(p.sale, out)
        self.save()
        self.on_stale()
        return p

    def _date_funding(self, sale, out):
        """Note which block the lock was mined in, or the height at which it
        was first seen unmined.

        The watcher calls a sale a ghost only when the chain says its funding
        is gone, and it can only say that about a funding it can place in time.
        Dating the lock the moment it is accepted is what keeps a sale that
        sells out between the lock and the first poll from being read as one
        that was never funded at all.
        """
        if sale.funding is None:
            return
        try:
            conf = int(out.get("confirmations") or 0)
            tip = int(self.rpc.chain_height())
        except Exception:
            return
        if conf < 1:
            sale.funding["seen_height"] = tip
            return
        try:
            height = tip - conf + 1
            block = self.rpc.call("getblockhash", height)
            if not block:
                raise PlatformError("no block at %s" % height)
            sale.funding["height"] = height
            sale.funding["block"] = block
        except Exception:
            sale.funding["mined"] = True     # mined, but the node did not say where

    def _find_lock(self, sale):
        """The outpoint resting at the sale address with the published amount,
        from the confirmed UTXO set."""
        try:
            res = self.rpc.call("scantxoutset", "start", ["raw(%s)" % sale.script_pubkey]) or {}
        except Exception as e:
            raise PlatformError("the node could not scan for the lock: %s" % e)
        if not res.get("success"):
            raise PlatformError("the node could not scan for the lock just now; "
                                "try again, or give the txid and output index")
        found = []
        for u in res.get("unspents") or []:
            if (u.get("asset") or "").lower() != sale.terms.token_asset:
                continue
            found.append((u.get("txid"), int(u.get("vout", 0)), _value_atoms(u, key="amount")))
        exact = [f for f in found if f[2] == sale.terms.total_atoms]
        if not exact:
            if found:
                raise PlatformError(
                    "an output of %s rests at the sale address, but the sale "
                    "publishes %s; send exactly the published allocation in one "
                    "output" % (sale.tokens(max(f[2] for f in found)),
                                sale.tokens(sale.terms.total_atoms)))
            raise PlatformError(
                "nothing is resting at the sale address in a confirmed block yet. "
                "If you sent the tokens a moment ago, wait for a confirmation or "
                "confirm the outpoint by txid and output index")
        return exact[0][0], exact[0][1]

    def lock_instructions(self, project):
        sale = project.sale
        return {
            "address": self.sale_address(sale),
            "script_pubkey": sale.script_pubkey,
            "asset": sale.terms.token_asset,
            "atoms": sale.terms.total_atoms,
            "amount": sale.tokens(sale.terms.total_atoms),
            "how": "send exactly this asset and amount to this address in one "
                   "output, then confirm the lock. Until then the sale is a "
                   "draft and nobody can buy.",
            "must_be_unblinded": "the locked output must be explicit. Sending to "
                                 "this unblinded address makes it so; do not send "
                                 "to a confidential form of it, because tokens "
                                 "locked into a confidential output can never be "
                                 "sold, only taken back by you after the close.",
            "only_the_token": "send nothing but the sale token to this address. "
                              "The sell leaf does not check what asset it is "
                              "spending, so any other asset resting here can be "
                              "taken by anyone at the sale's price.",
            "price_reduced": bool(getattr(project, "price_was_reduced", False)),
            "verify_against": "the terms in this response, not the ones you "
                              "submitted: the price is stored in lowest terms, "
                              "and the address is derived from the stored values.",
        }

    # --- buying -------------------------------------------------------------

    def plan_buy(self, account, slug, token_atoms=None, payment_atoms=None):
        p = self._project(slug)
        if p.sale is None:
            raise PlatformError("this project has no sale")
        standing = self.stake.standing(account)
        tier = self.stake.policy.for_stake(standing["stake_atoms"])
        height = self.height(strict=p.sale.close_is_height())
        plan = p.sale.plan_buy(account, tier, token_atoms=token_atoms,
                               payment_atoms=payment_atoms, height=height)
        out = plan.to_json()
        out["tier"] = tier.to_json()
        out["allowance_after_atoms"] = p.sale.allowance_for(account, tier) - plan.payment_atoms
        out["sale_address"] = self.sale_address(p.sale)
        out["cap"] = {
            "per_sale_atoms": tier.cap_atoms,
            "committed_atoms": p.sale.allocations.get(account, 0),
            "enforced_by": "levo",
            "note": "tier caps are Levo's allocation policy. The sale covenant "
                    "enforces the price, the treasury, the token and the minimum "
                    "lot; it does not enforce a per-buyer maximum.",
        }
        out["fee"] = self.fee_advice(p.sale, n_inputs=2)
        return out

    def fee_advice(self, sale, n_inputs=2, fee_asset=None, vsize=None, kind="buy"):
        """What a fee should be, from the node's own relay floor.

        The floor is in reference units per kvB; the fee is paid in an asset
        of the payer's choosing, so it is converted through the node's rate
        for that asset. The suggestion carries a margin over the floor because
        a fee exactly at the floor is the first thing dropped when the pool is
        busy.

        Sizing has to be honest in one direction: a figure BELOW the real size
        produces a "minimum" the node then rejects, and the buyer finds out
        only after signing. The constants below are measured against the
        builder's own estimate for the two transaction shapes Levo makes, and
        round up. Once a transaction actually exists its measured size is
        passed in and these are not used at all.
        """
        asset = (fee_asset or sale.terms.payment_asset).lower()
        if vsize is None:
            base = RECLAIM_BASE_VSIZE if kind == "reclaim" else BUY_BASE_VSIZE
            vsize = base + PER_INPUT_VSIZE * max(1, int(n_inputs))
        vsize = int(vsize)
        out = {"asset": asset, "vsize_estimate": vsize,
               "min_atoms": None, "suggested_atoms": None, "rate_atoms_per_kvb": None}
        if self.rpc is None:
            return out
        try:
            floor = self.rpc.min_relay_fee_atoms_per_kvb()
            rates = self.rpc.call("getfeeexchangerates") or {}
        except Exception:
            return out
        rate = _rate_for(rates, asset, self._labels())
        if floor is None or not rate:
            return out
        # floor is reference units per kvB; one atom of the asset is worth
        # rate / 1e8 reference units.
        per_kvb = -(-floor * 100_000_000 // int(rate))
        out["rate_atoms_per_kvb"] = per_kvb
        out["min_atoms"] = -(-vsize * per_kvb // 1000)
        out["suggested_atoms"] = max(out["min_atoms"] * 2, out["min_atoms"] + 1)
        return out

    def _labels(self):
        try:
            return self.rpc.call("dumpassetlabels") or {}
        except Exception:
            return {}

    def build_buy(self, account, slug, plan_json, buyer):
        """Turn a plan into the transaction that settles it."""
        p = self._project(slug)
        if p.sale is None or not p.sale.funding:
            raise PlatformError("this sale is not funded")
        if not isinstance(buyer, dict):
            raise PlatformError("buyer must be an object")
        standing = self.stake.standing(account)
        tier = self.stake.policy.for_stake(standing["stake_atoms"])
        height = self.height(strict=p.sale.close_is_height())
        plan = p.sale.plan_buy(account, tier,
                               token_atoms=plan_json.get("token_atoms"),
                               payment_atoms=plan_json.get("payment_atoms"),
                               height=height)
        # The outpoint the plan spends has to be unspent NOW, with the mempool
        # included. Between a buy being broadcast and the watcher noticing,
        # every transaction built against the old outpoint would be a double
        # spend the buyer signs for nothing.
        if self.rpc is not None:
            try:
                out = self.rpc.txout(p.sale.funding["txid"], p.sale.funding["vout"])
            except Exception as e:
                raise PlatformError("the Sequentia node could not be asked whether "
                                    "the sale's tokens are still resting: %s" % e)
            if out is None:
                self.on_stale()
                raise PlatformError(
                    "the sale's tokens have just moved: another purchase is "
                    "pending or has confirmed, and the sale carries on from a "
                    "new outpoint once it is seen. Price it again in a minute")
        buyer = dict(buyer)
        buyer["token_script_pubkey"] = self._spk(buyer, "token_script_pubkey",
                                                 "token_address", "where your tokens go")
        if buyer.get("change_address") or buyer.get("change_script_pubkey"):
            buyer["change_script_pubkey"] = self._spk(buyer, "change_script_pubkey",
                                                      "change_address", "where your change goes")
        else:
            # A buyer who names only where the tokens go means the same wallet
            # for the change. Refusing instead would be a refusal over a field
            # whose answer was already given, and change with nowhere to go is
            # burned value.
            buyer["change_script_pubkey"] = buyer["token_script_pubkey"]
        buyer["inputs"] = self.verify_buyer_inputs(buyer.get("inputs"))
        fee_asset = self.resolve_asset(buyer.get("fee_asset") or p.sale.terms.payment_asset)
        buyer["fee_asset"] = fee_asset
        self.check_fee_asset(fee_asset, buyer.get("fee_atoms"))
        self.check_fee_atoms(p.sale, buyer.get("fee_atoms"),
                             n_inputs=len(buyer["inputs"]), fee_asset=fee_asset)
        built = TX.build_buy(p.sale, plan, buyer)
        built["token_atoms"] = plan.token_atoms
        built["payment_atoms"] = plan.payment_atoms
        built["remainder_atoms"] = plan.remainder_atoms
        built["fee"] = self.fee_advice(p.sale, n_inputs=len(buyer["inputs"]),
                                       fee_asset=fee_asset,
                                       vsize=built.get("vsize_estimate"))
        built["fee"]["paying_atoms"] = int(buyer["fee_atoms"])
        return built

    def _spk(self, body, spk_key, addr_key, what):
        addr = body.get(addr_key)
        if addr:
            try:
                return ADDR.to_script_pubkey(addr, self.hrp).hex()
            except ValueError as e:
                raise PlatformError("%s: %s" % (what, e))
        spk = body.get(spk_key)
        if not spk:
            raise PlatformError("%s: give an address (%s) or a scriptPubKey (%s)"
                                % (what, addr_key, spk_key))
        if not isinstance(spk, str) or len(spk) % 2 or any(c not in "0123456789abcdefABCDEF" for c in spk):
            raise PlatformError("%s: %s must be a scriptPubKey in hex" % (what, spk_key))
        try:
            return ADDR.check_script_pubkey(spk, what)
        except ValueError as e:
            raise PlatformError(str(e))

    def build_reclaim(self, account, slug, body):
        """Sweep unsold tokens after the close. Only the project may ask."""
        with self.lock:
            return self._build_reclaim(account, slug, body)

    def _build_reclaim(self, account, slug, body):
        p = self._project(slug)
        if p.issuer_account != account:
            raise NotAuthorised("only the project's issuer can reclaim its sale")
        if not isinstance(body, dict):
            raise PlatformError("send the reclaim details as an object")
        sale = p.sale
        if sale is None or not sale.funding or sale.locked_atoms <= 0 \
                or sale.status in S.FINAL or sale.status == S.GHOST:
            raise PlatformError("this sale holds nothing to reclaim")
        if self.rpc is None:
            raise PlatformError("no node connection; cannot build a reclaim")
        if sale.close_is_height():
            if not sale.has_closed(height=self.height(strict=True)):
                raise PlatformError(
                    "this sale has not closed yet: it closes at block %d and "
                    "the chain is at %d. The covenant will reject a reclaim "
                    "before its close" % (sale.terms.close_locktime, self.height()))
        else:
            # The chain judges a time locktime against median time past, which
            # trails the wall clock by a few blocks. A reclaim built before the
            # chain's clock reaches the close is rejected as non-final.
            mtp = self.median_time()
            now = mtp if mtp is not None else time.time()
            if not sale.has_closed(now=now):
                wait = int(sale.terms.close_locktime - now)
                raise PlatformError(
                    "this sale has not closed yet by the chain's clock: %s to go. "
                    "The covenant will reject a reclaim before its close"
                    % _duration(max(wait, 60)))
        genesis = self.rpc.call("getblockhash", 0)
        dest = self._spk(body, "destination_script_pubkey", "destination_address",
                         "where the reclaimed tokens go")
        fee_inputs = self.verify_buyer_inputs(body.get("fee_inputs"))
        fee_asset = self.resolve_asset(body.get("fee_asset") or sale.terms.payment_asset)
        fee_atoms = body.get("fee_atoms")
        self.check_fee_asset(fee_asset, fee_atoms)
        self.check_fee_atoms(sale, fee_atoms, n_inputs=len(fee_inputs),
                             fee_asset=fee_asset, kind="reclaim")
        built = TX.build_reclaim(
            sale,
            destination_spk=dest,
            fee_inputs=fee_inputs,
            fee_atoms=fee_atoms,
            fee_asset=fee_asset,
            genesis_hash=genesis,
            locktime=body.get("locktime"))
        # The transaction id is fixed before any signature is added, so the
        # watcher can recognise this reclaim on chain by its output 0 and call
        # the sale reclaimed rather than merely empty.
        built["fee"] = self.fee_advice(sale, n_inputs=len(fee_inputs),
                                       fee_asset=fee_asset,
                                       vsize=built.get("vsize_estimate"),
                                       kind="reclaim")
        built["fee"]["paying_atoms"] = int(fee_atoms)
        sale.note_reclaim(built["txid"])
        self.save()
        return built

    def record_purchase(self, account, slug, txid, token_atoms, payment_atoms):
        """Record a purchase against the account's allocation for this sale.

        This writes the ALLOCATION LEDGER and nothing else. The watcher owns
        what the sale is -- how much is left, where it rests, whether it is
        finished -- because the chain is the only thing that actually knows,
        and purchases happen that never pass through here at all.

        The ledger is the one thing the per-buyer cap rests on, so it takes
        only what a purchase can honestly be: a named transaction, a positive
        amount, and never less than the covenant's price for the tokens named.
        A purchase whose treasury output has already been spent cannot be
        checked and is still recorded, because it happened and refusing would
        only cost the buyer their own headroom. One that visibly pays somebody
        else is refused, so a mistyped txid does not consume an allocation.
        """
        with self.lock:
            return self._record_purchase(account, slug, txid, token_atoms, payment_atoms)

    def _record_purchase(self, account, slug, txid, token_atoms, payment_atoms):
        p = self._project(slug)
        sale = p.sale
        if sale is None:
            raise PlatformError("this project has no sale")
        if sale.status in (S.DRAFT, S.GHOST):
            raise PlatformError("this sale is not funded, so nothing can have been bought")
        txid = str(txid or "").lower()
        if not TXID_RE.match(txid):
            raise PlatformError("txid must be the 64-hex id of the purchase transaction")
        try:
            token_atoms = S._atoms(token_atoms, "token_atoms")
            payment_atoms = S._atoms(payment_atoms, "payment_atoms")
        except S.SaleError as e:
            raise PlatformError(str(e))
        if token_atoms <= 0 or payment_atoms <= 0:
            raise PlatformError("a purchase has a positive token amount and a positive payment")
        standing = self.stake.standing(account)
        tier = self.stake.policy.for_stake(standing["stake_atoms"])
        if not tier.cap_atoms:
            raise NotAuthorised(
                "the ledger records a purchase against your allocation, and "
                "your tier has none. Stake to a tier that may buy, then record "
                "it -- the purchase itself is on chain either way, and the "
                "watcher reads the sale from there")
        elsewhere = any(e.get("txid") == txid
                        for acct, entries in sale.purchases.items()
                        if acct != account
                        for e in entries)
        if elsewhere:
            raise PlatformError(
                "that transaction is already recorded against another account. "
                "A purchase counts once, for whoever recorded it first")
        already = next((e for e in sale.purchases.get(account, []) if e.get("txid") == txid), None)
        if already is not None:
            # The same purchase, recorded again: it counted once and counts
            # once. Answer as the first call did rather than double the ledger.
            return {"recorded": True, "purchase": already, "already_recorded": True,
                    "treasury_payment_verified": already.get("verified"),
                    "committed_atoms": sale.allocations.get(account, 0),
                    "allowance_remaining_atoms": sale.allocation_remaining(account, tier),
                    "sale_status": "the watcher reads this from the chain; a purchase "
                                   "moves it whether or not it was recorded here"}
        if len(sale.purchases.get(account, [])) >= MAX_PURCHASES_PER_ACCOUNT:
            raise PlatformError(
                "this account has already recorded %d purchases of this sale, "
                "which is as many as the ledger keeps. The allocation ledger is "
                "Levo's cap bookkeeping; the purchases themselves are on chain"
                % MAX_PURCHASES_PER_ACCOUNT)
        if token_atoms > sale.terms.total_atoms:
            raise PlatformError(
                "that is more of the token than the sale ever held (%s), so no "
                "such purchase was made"
                % sale.tokens(sale.terms.total_atoms))
        if token_atoms < sale.terms.min_lot:
            raise PlatformError(
                "the covenant refuses a purchase below its minimum lot of %s, "
                "so no purchase of %s can have been made from this sale"
                % (sale.tokens(sale.terms.min_lot), sale.tokens(token_atoms)))
        # The covenant charged at least this much; a lower figure would be a
        # way to consume less headroom than the purchase used.
        payment_atoms = max(payment_atoms, sale.terms.cost_for(token_atoms))

        verified = None
        if self.rpc is not None:
            try:
                out = self.rpc.txout(txid, 0)
            except Exception:
                out = None
            if out is None and not self._node_has_seen(txid):
                # Recording spends the caller's own headroom in this sale, and
                # nothing gives it back. A transaction the node has never heard
                # of is far more likely to be a purchase that was never
                # broadcast -- or a typo -- than one already spent, and the
                # caller can record it the moment it exists.
                raise PlatformError(
                    "the node has not seen %s. Broadcast the purchase first, "
                    "then record it; recording spends your allocation in this "
                    "sale and cannot be undone" % txid)
            if out is not None:
                spk = ((out.get("scriptPubKey") or {}).get("hex") or "").lower()
                want = TX.treasury_script_pubkey(sale.terms).hex()
                verified = (spk == want)
                if verified:
                    # The treasury credit IS the payment. Reading it from the
                    # chain rather than from the caller keeps the ledger, and
                    # every cap measured against it, equal to what was paid.
                    paid = _value_atoms(out)
                    if paid and (out.get("asset") or "").lower() == sale.terms.payment_asset:
                        payment_atoms = max(payment_atoms, paid)
                if not verified:
                    raise PlatformError(
                        "transaction %s does not pay this sale's treasury at "
                        "output 0, so it is not a purchase of this sale" % txid)

        entry = sale.record_purchase(account, payment_atoms, token_atoms,
                                     txid=txid, verified=verified)
        # A partial buy re-rests the remainder at this transaction's output 1.
        # Telling the watcher where to look lets it see the remainder in the
        # mempool, so the sale moves as soon as the buy is broadcast.
        sale.expect_remainder_at(txid, 1)
        self.save()
        self.on_stale()
        return {
            "recorded": True,
            "purchase": entry,
            "treasury_payment_verified": verified,
            "committed_atoms": sale.allocations[account],
            "allowance_remaining_atoms": sale.allocation_remaining(account, tier),
            "sale_status": "the watcher reads this from the chain; a purchase "
                           "moves it whether or not it was recorded here",
        }

    # --- reads --------------------------------------------------------------

    def project(self, slug):
        """One listing by its page name, or NotFound."""
        return self._project(slug)

    @property
    def hrp(self):
        return self._hrp() if callable(self._hrp) else self._hrp

    def _project(self, slug):
        p = self.projects.get(slug)
        if p is None:
            raise NotFound("no such project")
        return p

    def public_projects(self, status=None, q=None, sort="new", limit=None, offset=0):
        """The board: everything but the long description, which the detail
        page carries.

        This is what every visitor downloads first, so it answers in pages and
        takes the question the reader is actually asking -- what can I buy now,
        what closes soonest, where is the one I am looking for -- rather than
        handing over every listing ever made and leaving the sorting to the
        browser.
        """
        h = self.height()
        now = self.median_time()
        wanted = (status or "all").lower()
        if wanted not in SALE_FILTERS:
            raise PlatformError("status must be one of: %s" % ", ".join(sorted(SALE_FILTERS)))
        sort = (sort or "new").lower()
        if sort not in SALE_SORTS:
            raise PlatformError("sort must be one of: %s" % ", ".join(SALE_SORTS))
        needle = str(q or "").strip().lower()[:80]
        items = []
        # A listing created or withdrawn while this runs would otherwise change
        # the map mid-iteration and take the whole board down with a 500.
        with self.lock:
            everything = list(self.projects.values())
        for p in everything:
            if p.hidden:
                continue
            if not self._matches(p, wanted, h, now):
                continue
            if needle and needle not in " ".join(
                    filter(None, [p.slug, p.name, p.ticker, p.summary])).lower():
                continue
            items.append(p)
        items.sort(key=self._order(sort, h, now))
        total = len(items)
        offset = max(0, int(offset or 0))
        # A page size is always applied. Without one the board hands back every
        # listing ever made, which is fine with two and not with two thousand.
        limit = DEFAULT_PAGE if limit is None else max(1, min(int(limit), MAX_PAGE))
        items = items[offset:offset + limit]
        out = []
        for p in items:
            d = p.to_json(height=h, now=now)
            d.pop("description", None)
            if p.sale:
                d["address"] = self.sale_address(p.sale)
            out.append(d)
        return {"projects": out, "total": total, "offset": offset,
                "limit": limit, "status": wanted, "sort": sort,
                "query": needle or None}

    def _matches(self, p, wanted, height, now):
        if wanted == "all":
            return True
        sale = p.sale
        shown = sale.shown_status(height=height, now=now) if sale else None
        if wanted == "open":
            return shown in (S.LIVE, S.PARTIAL)
        if wanted == "draft":
            return sale is None or shown in (S.DRAFT, S.GHOST)
        if wanted == "finished":
            return shown in (S.SOLD_OUT, S.CLOSED, S.RECLAIMED)
        return True

    def _order(self, sort, height, now):
        """How the board is sorted. Newest first by default; a sale that is
        about to close is the one a reader can still act on, so that is the
        other order worth having."""
        if sort == "closing":
            far = float("inf")

            def key(p):
                sale = p.sale
                if not sale or sale.shown_status(height=height, now=now) not in (S.LIVE, S.PARTIAL):
                    return (1, far, -p.created_at)
                return (0, sale.terms.close_locktime, -p.created_at)
            return key
        if sort == "progress":
            def key(p):
                sale = p.sale
                total = sale.terms.total_atoms if sale and sale.terms.total_atoms else 0
                share = (sale.sold_atoms / total) if (sale and total) else -1
                return (-share, -p.created_at)
            return key
        return lambda p: -p.created_at

    def set_visibility(self, account, slug, hidden=None, notice=None):
        """An operator's control over what this Levo advertises.

        It reaches exactly as far as the page: hiding a listing does not touch
        the covenant, which anyone can still buy from with the terms alone.
        Saying so plainly is the point -- an operator who could stop a sale
        would be a party to it.
        """
        if account not in self.operators:
            raise NotAuthorised("only an operator of this Levo can flag a listing")
        with self.lock:
            p = self._project(slug)
            if hidden is not None:
                p.hidden = bool(hidden)
            if notice is not None:
                p.notice = _text(notice, "notice", 400) or None
            self.save()
            return p

    def projects_of(self, account, limit=None, offset=0):
        h = self.height()
        out = []
        with self.lock:
            mine = sorted((p for p in self.projects.values()
                           if p.issuer_account == account),
                          key=lambda x: -x.created_at)
        total = len(mine)
        offset = max(0, int(offset or 0))
        limit = DEFAULT_PAGE if limit is None else max(1, min(int(limit), MAX_PAGE))
        for p in mine[offset:offset + limit]:
            d = p.to_json(height=h)
            if p.sale:
                d["address"] = self.sale_address(p.sale)
                d["lock"] = self.lock_instructions(p) if p.sale.status in (S.DRAFT, S.GHOST) else None
            out.append(d)
        return {"projects": out, "total": total, "offset": offset, "limit": limit}

    def positions(self, account, tier, limit=None, offset=0):
        """What this account has put into each sale, and what it may still put."""
        h = self.height()
        out = []
        with self.lock:
            everything = sorted(self.projects.values(), key=lambda x: -x.created_at)
        offset = max(0, int(offset or 0))
        limit = DEFAULT_PAGE if limit is None else max(1, min(int(limit), MAX_PAGE))
        seen = 0
        for p in everything:
            sale = p.sale
            if sale is None:
                continue
            committed = sale.allocations.get(account, 0)
            purchases = [e for e in sale.purchases.get(account, []) if not e.get("voided")]
            if not committed and not purchases:
                continue
            seen += 1
            if seen <= offset:
                continue
            if len(out) >= limit:
                continue
            out.append({
                "slug": p.slug, "name": p.name, "ticker": p.ticker,
                "decimals": p.decimals,
                "status": sale.shown_status(height=h),
                "committed_atoms": committed,
                "tokens_atoms": sum(int(e["token_atoms"]) for e in purchases),
                "cap_atoms": tier.cap_atoms,
                "allowance_remaining_atoms": sale.allowance_for(account, tier),
                # The newest first, and only a page of them: an account that has
                # bought a hundred times does not need all hundred to see where
                # it stands, and the whole ledger for one sale is its own route.
                "purchases": purchases[-PURCHASES_SHOWN:][::-1],
                "purchases_total": len(purchases),
            })
        return {"positions": out, "total": seen, "offset": offset, "limit": limit}

    def sale_address(self, sale):
        return ADDR.from_script_pubkey(sale.script_pubkey, self.hrp)

    def resolve_asset(self, asset, what="fee_asset"):
        """A 64-hex asset id from what the caller wrote.

        Wallets and the node show assets by label, so a caller naturally sends
        "USDX" where an id is meant. Taking the label here turns a transaction
        that would be built against the label as if it were an id -- and fail
        with a message about an asset nobody has -- into the asset they meant.
        """
        text = str(asset or "").strip()
        if ASSET_RE.match(text):
            return text.lower()
        # `dumpassetlabels` answers label -> asset id.
        for label, aid in (self._labels() or {}).items():
            if str(label).lower() == text.lower() and ASSET_RE.match(str(aid) or ""):
                return str(aid).lower()
        raise PlatformError(
            "%s: %r is neither a 64-character asset id nor a label this node "
            "knows. The node lists the labels it has (dumpassetlabels)."
            % (what, text))

    def check_fee_atoms(self, sale, fee_atoms, n_inputs, fee_asset, vsize=None,
                        kind="buy"):
        """A fee has to be a whole number of atoms, and it has to be one the
        network will actually take.

        A transaction below the node's relay floor is not rejected by Levo, it
        is rejected by every node it is offered to -- after the payer has
        signed it and gone looking for why nothing happened.
        """
        if fee_atoms is None or isinstance(fee_atoms, bool) or not isinstance(fee_atoms, int):
            raise PlatformError(
                "fee_atoms must be a whole number of atoms of the fee asset")
        if fee_atoms <= 0:
            raise PlatformError(
                "a transaction pays a fee: fee_atoms must be more than 0. Ask "
                "for the current figure and use its suggested_atoms")
        advice = self.fee_advice(sale, n_inputs=n_inputs, fee_asset=fee_asset,
                                 vsize=vsize, kind=kind)
        floor = advice.get("min_atoms")
        if floor and fee_atoms < floor:
            raise PlatformError(
                "a fee of %d atoms is below what this node will relay for a "
                "transaction of %d vB, which is %d atoms; the suggested fee is "
                "%d" % (fee_atoms, advice["vsize_estimate"], floor,
                        advice.get("suggested_atoms") or floor))
        return advice

    def _node_has_seen(self, txid):
        """Whether the node knows this transaction at all.

        Absence of an answer is NOT absence of the transaction, and most nodes
        cannot answer directly: without `-txindex`, `getrawtransaction` fails
        for anything that is not in the mempool, confirmed or invented alike.
        So the question is asked three ways, and only a no from all of them
        counts: is it in the mempool, does it still have an unspent output, and
        does the node happen to have an index that knows it.

        A purchase leaves the buyer their own tokens, so "some output of it is
        unspent" is true of every recent one. The only real purchase this
        refuses is one whose every output has already been spent, and the buyer
        of that one has moved their tokens on.
        """
        if self.rpc is None:
            return True
        try:
            if txid in (self.rpc.call("getrawmempool") or []):
                return True
        except Exception:
            return True                 # cannot tell: do not refuse a real buy
        for vout in range(OUTPUTS_LOOKED_AT):
            try:
                if self.rpc.txout(txid, vout) is not None:
                    return True
            except Exception:
                return True
        try:
            return bool(self.rpc.call("getrawtransaction", txid, True))
        except Exception:
            return False                # no index, no mempool, nothing unspent

    def check_fee_asset(self, asset, fee_atoms):
        """Refuse a fee in an asset this node will not take.

        Sequentia has an open fee market: a fee may be paid in any asset the
        network accepts, and nothing -- the policy asset included -- is the
        default. That freedom means a buyer can pick an asset nobody accepts,
        and the only feedback would be a relay rejection after they signed.
        """
        if not fee_atoms or self.rpc is None:
            return
        try:
            rates = self.rpc.call("getfeeexchangerates") or {}
        except Exception:
            return                      # cannot tell; let the chain decide
        if not rates:
            return
        if _rate_for(rates, asset, self._labels()) is not None:
            return
        raise PlatformError(
            "the node Levo reads does not accept fees in %s. This chain has an "
            "open fee market, so a fee may be paid in any accepted asset and "
            "none is the default; the node lists what it takes "
            "(getfeeexchangerates)." % asset)

    def describe_inputs(self, inputs):
        """Say, for each outpoint, whether a covenant purchase can spend it.

        A wallet knows what it holds but not what a covenant can read: a
        confidential output states no amount, and the sell leaf reads the
        amount it is spending. The wallet cannot tell those apart from its own
        records either, so the page asks here rather than guessing from an
        address form -- which is how a buyer ends up being told their ordinary
        funds are confidential.
        """
        out = []
        for i in (inputs if isinstance(inputs, list) else [])[:MAX_INPUTS]:
            if not isinstance(i, dict) or not i.get("txid") or i.get("vout") is None:
                raise PlatformError("each input needs a txid and a vout")
            txid = str(i["txid"]).lower()
            try:
                vout = int(i["vout"])
            except (TypeError, ValueError):
                raise PlatformError("input vout must be a whole number")
            if not TXID_RE.match(txid):
                raise PlatformError("input txid %r is not 64 hex characters" % i["txid"])
            row = {"txid": txid, "vout": vout, "spendable": False,
                   "asset": None, "atoms": None, "why": None}
            if self.rpc is None:
                row["why"] = "no node connection, so nothing can be checked"
                out.append(row)
                continue
            try:
                found = self.rpc.txout(txid, vout)
            except Exception as e:
                row["why"] = "the node could not be asked: %s" % e
                out.append(row)
                continue
            if found is None:
                row["why"] = "not an unspent output: it does not exist, or it is spent"
            elif found.get("valuecommitment") or found.get("amountcommitment") \
                    or found.get("assetcommitment"):
                row["why"] = ("confidential: it commits to an amount instead of "
                              "stating one, and a covenant reads the amount it spends")
            else:
                row["spendable"] = True
                row["asset"] = (found.get("asset") or "").lower()
                row["atoms"] = _value_atoms(found)
            out.append(row)
        return out

    def verify_buyer_inputs(self, inputs):
        """Check the buyer's funding outputs against the chain before building.

        The buyer says what they are spending; the chain says what is actually
        there. Trusting the claim produces a transaction that fails at relay
        with a message about sums that explains nothing, so each input is
        looked up and its asset, value and confidentiality taken from the node.
        """
        if inputs is None:
            inputs = []
        if not isinstance(inputs, list):
            raise PlatformError("inputs must be a list of {txid, vout}")
        if len(inputs) > MAX_INPUTS:
            raise PlatformError("at most %d inputs per transaction; consolidate first"
                                % MAX_INPUTS)
        seen = set()
        cleaned = []
        for i in inputs:
            if not isinstance(i, dict) or not i.get("txid") or i.get("vout") is None:
                raise PlatformError("each input needs a txid and a vout")
            txid = str(i["txid"]).lower()
            if not TXID_RE.match(txid):
                raise PlatformError("input txid %r is not 64 hex characters" % i["txid"])
            try:
                vout = int(i["vout"])
            except (TypeError, ValueError):
                raise PlatformError("input vout must be a whole number")
            if (txid, vout) in seen:
                raise PlatformError("input %s:%d is listed twice" % (txid, vout))
            seen.add((txid, vout))
            cleaned.append(dict(i, txid=txid, vout=vout))
        if self.rpc is None:
            return cleaned
        checked = []
        for i in cleaned:
            out = self.rpc.txout(i["txid"], i["vout"])
            if out is None:
                raise PlatformError(
                    "input %s:%s is not an unspent output -- it does not exist, "
                    "or it has already been spent" % (i["txid"], i["vout"]))
            blinded = bool(out.get("valuecommitment") or out.get("amountcommitment")
                           or out.get("assetcommitment"))
            asset = (out.get("asset") or "").lower()
            value = _value_atoms(out)
            spk = (out.get("scriptPubKey") or {}).get("hex")
            checked.append({"txid": i["txid"], "vout": i["vout"],
                            "asset": asset or str(i.get("asset", "")).lower(),
                            "value_atoms": int(value or 0),
                            "script_pubkey": spk,
                            "blinded": blinded})
        return checked

    def _issuer_standing(self, account):
        """The listing account's stake and tier, or nothing if the chain cannot
        be asked. Never a reason to fail a page: this is context, not a term."""
        out = {"account": account, "stake_atoms": None, "tier": None}
        try:
            standing = self.stake.standing(account)
        except Exception:
            return out
        out["stake_atoms"] = standing.get("stake_atoms")
        tier = standing.get("tier") or {}
        out["tier"] = tier.get("name")
        out["may_list"] = bool(tier.get("may_list"))
        return out

    def sale_ledger(self, account, slug, limit=None, offset=0):
        """Every purchase Levo recorded against this sale.

        The chain is reconcilable by anyone: the sale address is unblinded and
        every page links it. This is the other half -- what LEVO believes, and
        what each account has committed against its cap -- which the issuer
        needs to answer "did my purchase register?" without reading a state
        file on a server.
        """
        p = self._project(slug)
        if p.issuer_account != account and account not in self.operators:
            raise NotAuthorised("only the project's issuer, or an operator of "
                                "this Levo, can read a sale's ledger")
        sale = p.sale
        if sale is None:
            raise NotFound("this project has no sale")
        entries = []
        for acct, rows in sale.purchases.items():
            for e in rows:
                entries.append(dict(e, account=acct))
        entries.sort(key=lambda e: e.get("at") or 0, reverse=True)
        total = len(entries)
        offset = max(0, int(offset or 0))
        limit = DEFAULT_PAGE if limit is None else max(1, min(int(limit), MAX_PAGE))
        return {
            "slug": slug,
            "purchases": entries[offset:offset + limit],
            "total": total,
            "offset": offset,
            "limit": limit,
            "committed_atoms": dict(sale.allocations),
            "buyers": len([a for a, rows in sale.purchases.items() if rows]),
            "what_this_is": "Levo's own record, which is what the per-buyer "
                            "caps are measured against. The chain is the "
                            "authority on what the sale holds: a purchase made "
                            "without Levo moves the sale and appears nowhere "
                            "here.",
        }

    def project_detail(self, slug):
        p = self._project(slug)
        d = p.to_json(height=self.height())
        # Who is selling, in the only terms Levo can vouch for: what the chain
        # says their account has staked. It is the whole basis of the tier that
        # let them list, and a buyer deciding whether to send money to a
        # stranger has nothing else here to go on.
        d["issuer"] = self._issuer_standing(p.issuer_account)
        if p.sale:
            d["address"] = self.sale_address(p.sale)
            d["verify"] = {
                "how": "rebuild the sale address from these terms and compare it "
                       "to the scriptPubKey of the funded output; they must be "
                       "identical",
                "committed": list(C.COMMITTED_TERMS),
                "published": list(C.PUBLISHED_TERMS),
                "what_that_means": "the committed terms are compiled into the "
                                   "address, so changing any of them makes a "
                                   "different sale. The published ones are not: "
                                   "the amount for sale is what Levo checked "
                                   "when it accepted the lock, and the covenant "
                                   "simply sells whatever it holds.",
                "script_pubkey": p.sale.script_pubkey,
                "internal_key": "NUMS -- there is no key path, so the project "
                                "cannot spend the sale out from under buyers",
                "sell_leaf": p.sale.cov.sell_leaf.hex(),
                "reclaim_leaf": p.sale.cov.reclaim_leaf.hex(),
            }
        return d


def _value_atoms(out, key="value"):
    if out.get("valueatoms") is not None:
        return int(out["valueatoms"])
    if out.get("amountatoms") is not None:
        return int(out["amountatoms"])
    v = out.get(key)
    if v is None and key == "value":
        v = out.get("amount")
    return RPCMOD.to_atoms(v) if v is not None else None


def _rate_for(rates, asset, labels):
    """The node's rate for an asset given by id, resolving through labels."""
    a = str(asset).lower()
    for k, v in rates.items():
        if str(k).lower() == a:
            return v
    for label, asset_id in (labels or {}).items():
        if str(asset_id).lower() == a:
            for k, v in rates.items():
                if str(k).lower() == str(label).lower():
                    return v
    return None


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 3600:
        return "about %d minutes" % max(1, seconds // 60)
    if seconds < 86400:
        return "about %d hours" % (seconds // 3600)
    return "about %d days" % (seconds // 86400)
