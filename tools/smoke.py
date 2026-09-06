#!/usr/bin/env python3
"""Smoke-test a running Levo from the outside, the way a visitor meets it.

    python3 tools/smoke.py https://sequentiatestnet.com/levo

What it checks: health answers and is ok; every route paints without a
console error in a real browser; /sales lands on the board; the sitemap and
robots.txt answer and name the public sales; a sale page's social card names
its image by a full address; the responses carry the headers a public site
should. With a staking key it also signs in the way a person without a
browser wallet does -- by pasting a signature -- and reads the account page:

    LEVO_SIGN_WIF=<wif> SEQUENTIA_CLI=sequentia-cli python3 tools/smoke.py <url>

The key never leaves the machine: the challenge is signed with the node's
`signmessagewithprivkey` here and only the signature is sent. It needs a
Chromium (the render suite's finder is reused) and the levod/tests helpers.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "levod" / "tests"))
sys.path.insert(0, str(ROOT / "levod"))

import cdp  # noqa: E402
import test_render as R  # noqa: E402

ROUTES = ["/", "/projects", "/sales", "/how-it-works", "/launch", "/account",
          "/p/no-such-sale", "/nothing-here-at-all"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "levo-smoke"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, dict(r.headers), r.read()


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(sys.argv) == 2 else 2
    if not re.match(r"https?://", sys.argv[1]):
        sys.stderr.write("smoke.py takes one argument, the address of a running Levo, "
                         "such as https://sequentiatestnet.com/levo\n")
        return 2
    base = sys.argv[1].rstrip("/")
    passed, failed = 0, []

    def ok(cond, what, detail=""):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(what + ((": " + str(detail)[:200]) if detail else ""))

    # --- what a monitor asks -------------------------------------------------
    status, headers, body = get(base + "/api/health")
    health = json.loads(body)
    ok(health.get("ok") is True, "health says ok", json.dumps(health)[:300])
    ok((health.get("app") or {}).get("serving") is True, "the app is being served")
    ok(not (health.get("app") or {}).get("source_newer_than_bundle"), "the bundle is not older than the source")

    # --- what a crawler asks -------------------------------------------------
    status, _, robots = get(base + "/robots.txt")
    ok(status == 200 and b"Sitemap:" in robots, "robots.txt names the sitemap")
    status, _, smap = get(base + "/sitemap.xml")
    ok(status == 200 and b"/projects</loc>" in smap, "the sitemap lists the board")
    slugs = re.findall(rb"/p/([a-z0-9-]+)</loc>", smap)
    status, hdrs, feed = get(base + "/feed.xml")
    ok(status == 200 and hdrs.get("Content-Type", "").startswith("application/atom+xml") and b"<entry>" in feed,
       "the feed answers as Atom with entries")
    status, _, board = get(base + "/api/projects?status=all&limit=50")
    public = [p["slug"].encode() for p in json.loads(board)["projects"] if not p.get("hidden")]
    ok(set(slugs) == set(public), "and every public sale, and nothing else",
       "sitemap %s vs board %s" % (sorted(slugs), sorted(public)))

    # --- what a link previewer sees -----------------------------------------
    status, _, home = get(base + "/")
    m = re.search(rb'<link rel="apple-touch-icon"[^>]*href="([^"]+)"', home)
    ok(m is not None, "the app shell declares a touch icon")
    if m:
        href = m.group(1).decode()
        status, _, png = get(href if href.startswith("http") else base.split("/", 3)[0] + "//" + base.split("/", 3)[2] + href)
        ok(status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n", "and the touch icon answers as a PNG")
    m = re.search(rb'<link rel="manifest" href="([^"]+)"', home)
    ok(m is not None, "the app shell declares a web manifest")
    if m:
        href = m.group(1).decode()
        status, hdrs, _ = get(href if href.startswith("http") else base.split("/", 3)[0] + "//" + base.split("/", 3)[2] + href)
        ok(status == 200 and hdrs.get("Content-Type", "").startswith("application/manifest+json"),
           "and the manifest answers as a manifest")
    status, _, how = get(base + "/how-it-works")
    ok(b"<title>How it works \xc2\xb7 Levo</title>" in how, "a page of the app carries its own title for a previewer")
    if public:
        status, hdrs, page = get(base + "/p/" + public[0].decode())
        text = page.decode("utf-8", "replace")
        ok('<meta property="og:image" content="http' in text, "the sale page's card image is a full address")
        ok("og:url" in text, "and the card names the page's own address")
        ok("nosniff" in hdrs.get("X-Content-Type-Options", "") and hdrs.get("X-Frame-Options"),
           "the page carries the headers a public site should", hdrs)

    # --- what a visitor sees ------------------------------------------------
    chromium = R.find_chromium()
    if not chromium:
        failed.append("no chromium found; the browser checks did not run")
    else:
        page = cdp.Page(chromium)
        try:
            for route in ROUTES:
                page.go(base + route, settle=2.0)
                errs = page.errors()
                ok(not errs, "%s paints without a console error" % route, errs[:2])
            page.go(base + "/sales", settle=1.5)
            ok(str(page.eval("location.pathname")).endswith("/projects"), "/sales lands on the board")
            # --- signing in without a browser wallet, when a key is given ----
            wif = os.environ.get("LEVO_SIGN_WIF")
            if wif:
                cli = os.environ.get("SEQUENTIA_CLI", "sequentia-cli")
                extra = []
                if os.environ.get("SEQUENTIA_DATADIR"):
                    extra.append("-datadir=" + os.environ["SEQUENTIA_DATADIR"])
                page.go(base + "/account", settle=2.0)
                page.click("Sign a message to continue")
                page.wait_for("!!document.querySelector('#challenge')")
                challenge = page.eval("document.querySelector('#challenge').value")
                sig = subprocess.run([cli] + extra + ["signmessagewithprivkey", wif, challenge],
                                     capture_output=True, text=True, timeout=60).stdout.strip()
                page.fill("#sig", sig)
                page.eval("document.querySelector('#sig').closest('form').requestSubmit()")
                try:
                    page.wait_for("document.body.innerText.includes('Your positions')", timeout=25)
                    ok(True, "signing in by pasting a signature reaches the account page")
                except Exception as e:
                    ok(False, "signing in by pasting a signature reaches the account page", e)
                ok(not page.errors(), "with no console error", page.errors()[:2])
        finally:
            page.stop()

    for f in failed:
        print("FAIL", f)
    print("%d passed, %d failed" % (passed, len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
