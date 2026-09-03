"""A very small Chrome DevTools Protocol client, in the standard library.

The render test paints pages and reads the pixels, which catches a white
screen and nothing else: it cannot type, click, or watch a request go out. The
buy flow -- the one path where a person authorises a payment -- is a sequence
of interactions, and nothing in this repository has ever driven it in a
browser.

This is the smallest thing that can: launch Chromium with its debugging port
open, speak WebSocket to the page target by hand, and expose evaluate, click
and wait. It is not a browser automation library and should not grow into one.
Everything it does is one round trip: send a JSON message with an id, read
frames until the reply with that id arrives.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


class BrowserError(RuntimeError):
    pass


class WS:
    """The client half of RFC 6455, for one connection to one page.

    Only what a debugging session needs: text frames, masked on the way out,
    unmasked on the way in, with continuation and ping handled because Chromium
    sends both. No extensions, no compression.
    """

    def __init__(self, url, timeout=30):
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=10)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)).encode())
        head = self._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise BrowserError("the browser refused the debugging socket: %r" % head[:120])
        want = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if want.lower().encode() not in head.lower():
            raise BrowserError("the debugging socket did not accept the handshake")
        self.buf = b""

    def _read_until(self, marker):
        data = b""
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("the browser closed the debugging socket")
            data += chunk
        return data

    def _recv(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BrowserError("the browser closed the debugging socket")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode()
        n = len(payload)
        header = bytes([0x81])
        mask = os.urandom(4)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv(self):
        """One complete text message, reassembled across continuation frames."""
        parts = []
        while True:
            b0, b1 = self._recv(2)
            final, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv(8))[0]
            if b1 & 0x80:                     # a server frame should not be masked
                self._recv(4)
            body = self._recv(n) if n else b""
            if opcode == 0x9:                 # ping: answer it or be closed
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0x8:
                raise BrowserError("the browser closed the debugging socket")
            if opcode in (0x1, 0x0):
                parts.append(body)
                if final:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class Page:
    """One page in a headless Chromium, driven over the protocol."""

    def __init__(self, chromium, port=None, timeout=30):
        self.dir = tempfile.mkdtemp(prefix="levo-cdp-")
        self.port = port or _free_port()
        self.log = open(os.path.join(self.dir, "chrome.log"), "w+")
        self.proc = subprocess.Popen(
            [chromium, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-first-run", "--disable-extensions", "--window-size=1280,1600",
             "--remote-debugging-port=%d" % self.port,
             "--user-data-dir=" + os.path.join(self.dir, "profile"),
             "about:blank"],
            stdout=self.log, stderr=subprocess.STDOUT)
        target = self._wait_for_target()
        self.ws = WS(target, timeout=timeout)
        self._id = 0
        self.console = []
        self.send("Runtime.enable")
        self.send("Page.enable")

    def _wait_for_target(self):
        base = "http://127.0.0.1:%d" % self.port
        for _ in range(120):
            try:
                pages = json.loads(urllib.request.urlopen(base + "/json/list", timeout=2).read())
                for p in pages:
                    if p.get("type") == "page" and p.get("webSocketDebuggerUrl"):
                        return p["webSocketDebuggerUrl"]
            except Exception:
                if self.proc.poll() is not None:
                    self.log.seek(0)
                    raise BrowserError("chromium exited:\n" + self.log.read()[-2000:])
            time.sleep(0.25)
        raise BrowserError("chromium never opened a debugging port")

    def send(self, method, **params):
        self._id += 1
        mine = self._id
        self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.consoleAPICalled":
                args = msg["params"].get("args") or []
                self.console.append((msg["params"].get("type"), " ".join(
                    str(a.get("value", a.get("description", ""))) for a in args)))
                continue
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"].get("exceptionDetails") or {}
                self.console.append(("exception", d.get("text", "") + " " + str(
                    (d.get("exception") or {}).get("description", ""))))
                continue
            if msg.get("id") != mine:
                continue
            if "error" in msg:
                raise BrowserError("%s: %s" % (method, msg["error"].get("message")))
            return msg.get("result", {})

    def on_new_document(self, source):
        """Run this before anything the page loads -- which is what a wallet
        extension does, and the only way to be one."""
        self.send("Page.addScriptToEvaluateOnNewDocument", source=source)

    def go(self, url, settle=1.0):
        self.send("Page.navigate", url=url)
        self.wait_for("document.readyState === 'complete'", timeout=30)
        time.sleep(settle)                    # the app's first fetches

    def eval(self, expr, timeout=30):
        r = self.send("Runtime.evaluate", expression="(function(){%s})()" % expr
                      if expr.strip().startswith("return") else expr,
                      returnByValue=True, awaitPromise=True, timeout=timeout * 1000)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise BrowserError("the page threw: %s" %
                               ((d.get("exception") or {}).get("description") or d.get("text")))
        return (r.get("result") or {}).get("value")

    def text(self):
        return self.eval("document.body ? document.body.innerText : ''")

    def wait_for(self, expr, timeout=20, every=0.2):
        """Wait for a JavaScript expression to be true, and say what the page
        looked like when it never was."""
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                last = self.eval(expr)
                if last:
                    return last
            except BrowserError as e:
                last = str(e)
            time.sleep(every)
        raise BrowserError("waited %ss for %s (last: %r)" % (timeout, expr, last))

    def click(self, text, kind="button", timeout=20):
        """Click the first control whose visible text contains `text`."""
        js = """
        (function(){
          const wanted = %s.toLowerCase();
          const all = Array.from(document.querySelectorAll(%s));
          const el = all.find((e) => (e.innerText||e.value||'').toLowerCase().includes(wanted)
                                     && !e.disabled && e.offsetParent !== null);
          if (!el) return false;
          el.scrollIntoView();
          el.click();
          return true;
        })()""" % (json.dumps(text), json.dumps(kind + ", a, [role=radio]"))
        self.wait_for(js, timeout=timeout)

    def fill(self, selector, value):
        self.eval("""
        (function(){
          const el = document.querySelector(%s);
          if (!el) throw new Error('no field ' + %s);
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
          setter.call(el, %s);
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()""" % (json.dumps(selector), json.dumps(selector), json.dumps(str(value))))

    def errors(self):
        """Console errors and exceptions, without the browser's own noise."""
        noise = ("favicon", "Failed to load resource: the server responded with a status of 404")
        return [(k, t) for k, t in self.console
                if k in ("error", "exception") and not any(n in t for n in noise)]

    def stop(self):
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except Exception:
                self.proc.kill()
            self.log.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
