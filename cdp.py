#!/usr/bin/env python3
"""Opt-in Chrome DevTools Protocol client — stdlib only, no dependencies.

CDP is the capability tier above AppleScript. With Chrome launched on a debug
port it gives a cleaner read path: run JS in the real tab (Runtime.evaluate),
grab a focus-race-free screenshot (Page.captureScreenshot), and reach the data
a single-page app already fetched. It is strictly opt-in — Chrome must be
started with --remote-debugging-port — so the safe-by-default Handle never
depends on it.

This module never writes to the page. It evaluates read expressions and
captures images, nothing more. Enable with:

    /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \\
        --remote-debugging-port=9222

(Chrome only accepts the flag from a cold start. A second isolated instance
needs its own --user-data-dir.)
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.request
from urllib.parse import urlparse

DEFAULT_PORT = 9222


def version(port: int = DEFAULT_PORT, timeout: float = 1.0) -> dict | None:
    """Browser version/metadata, or None if no debug port is listening."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def targets(port: int = DEFAULT_PORT, timeout: float = 1.0) -> list[dict]:
    """Open page targets (tabs) with their webSocketDebuggerUrl."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=timeout
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    return [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def target_for_url(url: str, port: int = DEFAULT_PORT) -> dict | None:
    """The page target whose URL matches `url` (exact, then prefix)."""
    ts = targets(port)
    for t in ts:
        if t.get("url") == url:
            return t
    for t in ts:
        if url and t.get("url", "").startswith(url.rstrip("/")):
            return t
    return None


# --------------------------------------------------------------------------- #
# minimal RFC6455 WebSocket client — text frames, one in-flight call at a time
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self, ws_url: str, timeout: float = 10.0):
        u = urlparse(ws_url)
        self.host = u.hostname
        self.port = u.port or 80
        self.path = u.path + (f"?{u.query}" if u.query else "")
        self.sock = socket.create_connection((self.host, self.port), timeout)
        self.sock.settimeout(timeout)
        self._handshake()
        self._buf = b""

    def _handshake(self):
        # a fixed key is fine; we don't validate the server's accept hash
        key = base64.b64encode(b"handle-cdp-0x13").decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP websocket handshake failed")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError("CDP websocket did not upgrade")

    def _send_text(self, text: str):
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text opcode
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_frame(self) -> str:
        def need(n):
            while len(self._buf) < n:
                chunk = self.sock.recv(8192)
                if not chunk:
                    raise ConnectionError("CDP websocket closed")
                self._buf += chunk
        need(2)
        b1, b2 = self._buf[0], self._buf[1]
        masked = b2 & 0x80
        ln = b2 & 0x7F
        off = 2
        if ln == 126:
            need(4)
            ln = struct.unpack(">H", self._buf[2:4])[0]
            off = 4
        elif ln == 127:
            need(10)
            ln = struct.unpack(">Q", self._buf[2:10])[0]
            off = 10
        mask = b""
        if masked:
            need(off + 4)
            mask = self._buf[off:off + 4]
            off += 4
        need(off + ln)
        data = bytearray(self._buf[off:off + ln])
        self._buf = self._buf[off + ln:]
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return bytes(data).decode("utf-8", "replace")

    def call(self, method: str, params: dict | None = None, _id: int = 1) -> dict:
        self._send_text(json.dumps({"id": _id, "method": method, "params": params or {}}))
        # skip event frames; return the matching response
        for _ in range(200):
            msg = json.loads(self._recv_frame())
            if msg.get("id") == _id:
                return msg
        raise TimeoutError(f"no CDP response for {method}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def evaluate(ws_url: str, expression: str) -> str | None:
    """Run a read-only JS expression in the target, return its string value."""
    ws = _WS(ws_url)
    try:
        resp = ws.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "timeout": 8000,
        })
        result = resp.get("result", {}).get("result", {})
        return result.get("value")
    finally:
        ws.close()


# Scroll the whole page (triggering lazy/scroll-reveal content), nudge common
# reveal-on-scroll patterns visible, then return to top — so a full-page capture
# isn't blank below the fold. Awaited as a promise via Runtime.evaluate.
_REVEAL_SCROLL_JS = """new Promise((resolve)=>{
  try { document.querySelectorAll('.reveal,[data-reveal]').forEach(e=>e.classList.add('in')); } catch(e){}
  let y=0; const vh=window.innerHeight||900; let steps=0;
  const tick=()=>{
    window.scrollTo(0,y); y+=vh; steps++;
    if (y < document.documentElement.scrollHeight && steps < 80) { setTimeout(tick, 55); }
    else { setTimeout(()=>{ window.scrollTo(0,0); setTimeout(resolve, 180); }, 120); }
  };
  tick();
})"""


def screenshot(ws_url: str, full: bool = False) -> bytes | None:
    """PNG bytes via Page.captureScreenshot.

    full=False captures the viewport. full=True scrolls the page first (so
    lazy/scroll-reveal sections render), measures the real content size, and
    captures the entire page in one shot.
    """
    ws = _WS(ws_url, timeout=25.0)
    try:
        if not full:
            resp = ws.call("Page.captureScreenshot", {"format": "png"})
            data = resp.get("result", {}).get("data")
            return base64.b64decode(data) if data else None
        ws.call("Runtime.enable")
        try:
            ws.call("Runtime.evaluate",
                    {"expression": _REVEAL_SCROLL_JS, "awaitPromise": True, "returnByValue": True})
        except Exception:
            pass  # capture what we can even if the scroll script misbehaves
        params = {"format": "png", "captureBeyondViewport": True}
        m = ws.call("Page.getLayoutMetrics").get("result", {})
        cs = m.get("cssContentSize") or m.get("contentSize") or {}
        w, h = int(cs.get("width", 0)), int(cs.get("height", 0))
        if w and h:
            params["clip"] = {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}
        resp = ws.call("Page.captureScreenshot", params)
        data = resp.get("result", {}).get("data")
        return base64.b64decode(data) if data else None
    finally:
        ws.close()
