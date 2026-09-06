#!/usr/bin/env python3
"""Does the app actually render?

Every other test here reads code or talks to the API. None of them opens a
page, so a change that leaves the bundle building and the routes answering can
still ship a white screen: a component that throws on mount, a store field
renamed on one side, a page that crashes when a sale has no funding. That is a
whole class of failure the gate could not see.

This starts the demo server -- the real levod over a stub node, with two seeded
sales -- drives a headless Chromium over every route, and fails on a console
error, an unhandled rejection, an empty page, or a React error boundary. It
reports itself skipped when no Chromium is on this machine, the way the node
test does without sequentiad.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

import cdp  # noqa: E402

CHROMIUM_CANDIDATES = [
    os.environ.get("LEVO_CHROMIUM"),
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    str(Path.home() / ".cache/ms-playwright/chromium-1228/chrome-linux64/chrome"),
    str(Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
]

# Every route the app serves. A page that mounts nothing still answers 200 and
# still has no console errors, so what is checked is that it PAINTED: the
# fraction of the screenshot that is not the colour of its own corner.
ROUTES = ["/", "/projects", "/how-it-works", "/launch", "/account",
          "/p/helios-grid", "/p/meridian-salt", "/p/no-such-sale", "/nothing-here-at-all"]

# The emptiest real page here is the 404, which is a heading, a line and a
# button on a dark ground. Anything below this painted nothing.
MIN_INK = 0.02


def find_chromium():
    for c in CHROMIUM_CANDIDATES:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Demo:
    """The real server over a stub node, on a port of its own."""

    def __init__(self, port):
        env = dict(os.environ, LEVOD_PORT=str(port), LEVOD_HOST="127.0.0.1",
                   LEVOD_STATE=str(Path(tempfile.mkdtemp()) / "state.json"),
                   LEVOD_SECRET="render-test-secret")
        self.log = open(os.path.join(tempfile.mkdtemp(), "demo.log"), "w+")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "levod" / "demo.py")],
                                     stdout=self.log, stderr=subprocess.STDOUT, env=env)
        self.base = "http://127.0.0.1:%d" % port
        self.state = env["LEVOD_STATE"]
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=2).read()
                return
            except Exception:
                if self.proc.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError("the demo server exited:\n" + self.log.read())
                time.sleep(0.25)
        raise RuntimeError("the demo server did not start")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
        self.log.close()


def ink(png_path):
    """How much of a screenshot is not the colour of its own corner.

    A page that mounted nothing is one flat rectangle, and so is a page whose
    root component threw. Reading the pixels is the only way to tell from the
    outside: `--dump-dom` never returns in some headless builds, and a
    screenshot is the one thing every build does the same.
    """
    import struct
    import zlib
    raw = Path(png_path).read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width = 8, b"", 0
    height = bpp = 0
    while pos < len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if depth != 8:
                raise ValueError("expected 8 bits a channel")
            bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    data = zlib.decompress(idat)
    stride = width * bpp
    out, prev = [], bytearray(stride)
    at = 0
    for _ in range(height):
        filt = data[at]; at += 1
        line = bytearray(data[at:at + stride]); at += stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if filt == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out.append(bytes(line))
        prev = line
    corner = out[0][:bpp]
    different = 0
    total = 0
    for y in range(0, height, 4):                 # every fourth row is plenty
        row = out[y]
        for x in range(0, width * bpp, bpp * 4):
            total += 1
            if row[x:x + bpp] != corner:
                different += 1
    return different / max(1, total)


def render(chromium, url, out_dir, name):
    """Paint a page, and come back with how much of it is not blank and what it
    complained about. Chromium's own noise about fonts and GPUs is left out; a
    page's own errors are not."""
    shot = os.path.join(out_dir, name + ".png")
    # No --user-data-dir on purpose: a fresh profile directory makes some
    # headless builds hang after the screenshot is written, which reads as the
    # page hanging. Pages are rendered one at a time, so the default profile is
    # never contended.
    proc = subprocess.run(
        [chromium, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--window-size=1200,1400",
         "--virtual-time-budget=8000", "--enable-logging=stderr", "--v=0",
         "--screenshot=" + shot, url],
        capture_output=True, text=True, timeout=120)
    noise = ("favicon", "GPU", "dbus", "Fontconfig", "DevTools", "sandbox",
             "gl_display", "viz", "Vulkan", "egl")
    complaints = [line.strip() for line in (proc.stderr or "").splitlines()
                  if re.search(r"Uncaught|unhandled|SEVERE", line)
                  and not any(n in line for n in noise)]
    painted = ink(shot) if os.path.isfile(shot) else 0.0
    return painted, complaints, shot


def main():
    chromium = find_chromium()
    if not chromium:
        print("no chromium found; skipping the render test "
              "(set LEVO_CHROMIUM to one)")
        return 0
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        print("web/dist is not built; skipping the render test "
              "(npm --prefix web run build)")
        return 0

    port = free_port()
    demo = Demo(port)
    out_dir = tempfile.mkdtemp(prefix="levo-render-")
    failed = []
    passed = 0
    try:
        # The demo keeps its state where it was told to, and nowhere else.
        # It used to put every demo on one fixed path and delete that path on
        # the way in, so a second demo -- this suite beside a demo somebody
        # was reading -- emptied the first one's state before the lock on
        # the file could refuse it.
        if Path(demo.state).is_file():
            passed += 1
        else:
            failed.append("the demo did not write its state where LEVOD_STATE said: %s" % demo.state)
        stray = Path(tempfile.gettempdir()) / "levo-demo-state.json"
        if not (stray.is_file() and stray.stat().st_mtime > time.time() - 60):
            passed += 1
        else:
            failed.append("the demo touched the default state path although it was told another")
        for path in ROUTES:
            name = path.strip("/").replace("/", "-") or "home"
            painted, complaints, shot = render(chromium, demo.base + path, out_dir, name)
            if painted < MIN_INK:
                failed.append("%s painted almost nothing (%.3f of the page)"
                              % (path, painted))
            else:
                passed += 1
            if complaints:
                failed.append("%s logged %s" % (path, complaints[:2]))
            else:
                passed += 1
        # --- a page left open stays true -----------------------------------
        #
        # The sale page used to read its sale once. A sale that closed while
        # the page was on screen went on offering the buy panel, and an edit
        # made elsewhere never appeared. It reads again every half minute now,
        # so an edit made through the API shows up on a page that was opened
        # before it, without a reload.
        # --- a sale that holds nothing says so, in the past tense -----------
        page = cdp.Page(chromium)
        try:
            page.go(demo.base + "/p/meridian-salt", settle=1.5)
            text = page.text()
            for want in ("Last rested at", "Reclaimed in", "Nothing rests at the address now",
                         "taken back what did not sell"):
                if want in text:
                    passed += 1
                else:
                    failed.append("the reclaimed sale's page does not say %r" % want)
            if "sits now" in text or "are locked under exactly these terms" in text:
                failed.append("the reclaimed sale's page still speaks of the covenant in the present")
            else:
                passed += 1
        finally:
            page.stop()
        # --- the board answers to the name the site gives it ----------------
        page = cdp.Page(chromium)
        try:
            page.go(demo.base + "/sales", settle=1.0)
            where = page.eval("location.pathname")
            if str(where).endswith("/projects"):
                passed += 1
            else:
                failed.append("/sales landed on %s rather than the board" % where)
        finally:
            page.stop()
        import json as _json
        import urllib.request as _url
        import signhelper as SH
        page = cdp.Page(chromium)
        try:
            page.go(demo.base + "/p/helios-grid", settle=1.5)
            before = page.eval("(function(){const e=document.querySelector('.hero-lede'); return e? e.innerText:''})()")
            def call(method, path, body=None, token=None):
                req = _url.Request(demo.base + path, data=_json.dumps(body).encode() if body is not None else None,
                                   method=method, headers={"Content-Type": "application/json",
                                                           **({"Authorization": "Bearer " + token} if token else {})})
                with _url.urlopen(req, timeout=10) as r:
                    return _json.loads(r.read())
            sec = 0x51ee0000000000000000000000000000000000000000000000000000000000a1   # the demo's issuer
            ch = call("POST", "/api/auth/challenge")
            tok = call("POST", "/api/auth/verify", {"message": ch["message"],
                                                   "signature": SH.sign_recoverable(sec, ch["message"])})["token"]
            call("PATCH", "/api/projects/helios-grid", {"summary": "Edited while the page was open."}, token=tok)
            shown = None
            for _ in range(40):
                time.sleep(1)
                shown = page.eval("(function(){const e=document.querySelector('.hero-lede'); return e? e.innerText:''})()")
                if shown == "Edited while the page was open.":
                    break
            if shown == "Edited while the page was open." and before != shown:
                passed += 1
            else:
                failed.append("a sale page left open did not pick up an edit within 40s (shows %r)" % shown)
            call("PATCH", "/api/projects/helios-grid", {"summary": before}, token=tok)
        finally:
            page.stop()

        # --- a levod that cannot write its record says so on the page -------
        #
        # The state directory goes read-only, a write is made through the API so
        # the save fails, and the app -- which reads health on load -- has to
        # show the banner. Then the directory is put back so the demo can end.
        import os as _os
        import stat as _stat
        state_dir = Path(demo.state).parent
        _os.chmod(state_dir, _stat.S_IRUSR | _stat.S_IXUSR)
        page = cdp.Page(chromium)
        try:
            ch = call("POST", "/api/auth/challenge")
            tok = call("POST", "/api/auth/verify", {"message": ch["message"],
                                                   "signature": SH.sign_recoverable(sec, ch["message"])})["token"]
            call("PATCH", "/api/projects/helios-grid", {"summary": "Written while the disk was read-only."}, token=tok)
            # Health answers 503 while the file cannot be written, which is
            # the point; urllib treats that as an error to raise, so read it.
            try:
                h = call("GET", "/api/health")
            except urllib.error.HTTPError as e:
                h = _json.loads(e.read().decode("utf-8", "replace"))
            if h.get("state_file", {}).get("writable") is False:
                passed += 1
            else:
                failed.append("health did not report an unwritable state file: %r" % h.get("state_file"))
            page.go(demo.base + "/", settle=1.5)
            text = page.eval("document.body.innerText")
            if "cannot write its own record" in text:
                passed += 1
            else:
                failed.append("the page did not say the record cannot be written")
        finally:
            page.stop()
            _os.chmod(state_dir, _stat.S_IRWXU)
            call("PATCH", "/api/projects/helios-grid", {"summary": before}, token=tok)

        # --- and the same pages on a phone ---------------------------------
        #
        # A screenshot says a page painted; it does not say the words on it can
        # be read. These two could not: four tier names plotted along an axis
        # overprinted into one word at 320px, and the display face put the full
        # stop of the heading on a line of its own.
        try:
            page = cdp.Page(chromium)
            try:
                # 300, not 320: a phone at 320 with a visible scrollbar lays
                # out in about 305, and that is where this first broke.
                page.send("Emulation.setDeviceMetricsOverride", width=300,
                          height=1200, deviceScaleFactor=1, mobile=True)
                page.go(demo.base + "/", settle=2.0)
                # A heading that wraps is fine; a line with nothing on it but
                # the full stop is what a display face this wide does at 320px
                # when its size does not follow the viewport.
                # A Range over the text, not the element: an element has one
                # box however many lines its text takes, and it is the LINES
                # that are being measured.
                runt = page.eval(
                    "(function(){const h=document.querySelector('.hero h1');"
                    "if(!h) return 0;"
                    "const r=document.createRange(); r.selectNodeContents(h);"
                    "const boxes=Array.from(r.getClientRects()).filter(b=>b.width>0);"
                    "return boxes.length ? Math.round(Math.min.apply(null,"
                    " boxes.map(x=>x.width))) : 0})()")
                over = page.eval(
                    "(function(){const h=document.querySelector('.hero h1');"
                    "return h ? h.scrollWidth - h.clientWidth : 0})()")
                if over and over > 1:
                    failed.append("the heading overflows its column by %spx at 300px" % over)
                elif runt and runt < 30:
                    failed.append("the heading breaks onto a line %spx wide at 300px" % runt)
                else:
                    passed += 1
                # The header keeps the page's gutter when its links wrap onto
                # their own row: a padding shorthand once zeroed it, and the
                # wordmark sat on the screen's edge above a margined page.
                edge = page.eval(
                    "(function(){const b=document.querySelector('.nav .brand');"
                    "const l=document.querySelector('.nav-links');"
                    "const h=document.querySelector('.hero h1');"
                    "if(!b||!l||!h) return 'missing';"
                    "const x=h.getBoundingClientRect().left;"
                    "return JSON.stringify([Math.round(b.getBoundingClientRect().left-x),"
                    " Math.round(l.getBoundingClientRect().left-x)])})()")
                # Every link in the header stays on one line: the row wraps
                # whole links, never the words inside one.
                broken = page.eval(
                    "(function(){const out=[];"
                    "for(const a of document.querySelectorAll('.nav-links a')){"
                    "const r=document.createRange(); r.selectNodeContents(a);"
                    "const lines=new Set(Array.from(r.getClientRects()).filter(b=>b.width>0).map(b=>Math.round(b.top)));"
                    "if(lines.size>1) out.push(a.innerText.trim())} return JSON.stringify(out)})()")
                if json.loads(broken or "[]"):
                    failed.append("a header link breaks across lines at 300px: %s" % broken)
                else:
                    passed += 1
                if edge == "missing":
                    failed.append("the header or the hero heading was not found at 300px")
                else:
                    off = [abs(v) for v in json.loads(edge)]
                    if max(off) > 1:
                        failed.append("at 300px the header sits %spx off the page's gutter" % off)
                    else:
                        passed += 1
                # A listing's own words, in a row whose width a lister does
                # not control: eighty characters with no spaces in them is a
                # name levod accepts, and it pushed the whole board sideways.
                page.go(demo.base + "/projects", settle=1.5)
                spill = page.eval(
                    "(function(){const r=document.querySelector('.row-name');"
                    "if(!r) return 0; r.firstChild.textContent='W'.repeat(80);"
                    "const d=document.documentElement;"
                    "return d.scrollWidth - d.clientWidth})()")
                if spill and spill > 1:
                    failed.append("a long name pushes the board %spx sideways" % spill)
                else:
                    passed += 1
                page.go(demo.base + "/", settle=1.5)
                shape = page.eval(
                    "(function(){const l=document.querySelector('.beam-list li');"
                    "const p=document.querySelector('.beam-labels');"
                    "return JSON.stringify([l?getComputedStyle(l).display:'none',"
                    "p?getComputedStyle(p).display:'none'])})()")
                listed, plotted = json.loads(shape or '[\"none\",\"none\"]')
                if listed == "none" or plotted != "none":
                    failed.append("at 300px the tiers are plotted (%s) rather than listed (%s)"
                                  % (plotted, listed))
                else:
                    passed += 1
            finally:
                page.stop()
        except Exception as e:
            failed.append("the narrow-screen checks could not run: %s" % e)
    finally:
        demo.stop()
        shutil.rmtree(out_dir, ignore_errors=True)

    for f in failed:
        print("  FAIL %s" % f)
    print("%d passed, %d failed" % (passed, len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
